"""Unified guardrail gateway."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from typing_extensions import override

if TYPE_CHECKING:
    from penguiflow.steering.guard_inbox import (
        SteeringGuardInbox,
        SteeringGuardResponse,
    )

    from .protocols import GuardrailRule, RiskRouter
    from .registry import RuleRegistry
from .context import (
    ContextSnapshotBuilder,
    ContextSnapshotV1,
    GuardrailContext,
    GuardrailEvent,
)
from .models import (
    GuardrailAction,
    GuardrailDecision,
    GuardrailSeverity,
    PauseSpec,
    RedactionSpec,
    StopSpec,
)
from .protocols import DecisionPolicy
from .routing import DefaultRiskRouter, RiskRoutingDecision

logger = logging.getLogger(__name__)


@dataclass
class GatewayConfig:
    """Gateway configuration.

    Attributes:
        mode: ``"enforce"`` applies decisions; ``"shadow"`` only
            evaluates them.
        sync_timeout_ms: Time limit for each FAST rule.
        sync_parallel: Run FAST rules concurrently.
        async_enabled: Submit events to the guard inbox for DEEP rules.
        sync_fail_open: When a FAST rule errors or times out, skip it
            (``True``) or STOP the run (``False``).
        async_fail_open: When the guard inbox fails (``submit`` or
            ``await_response`` raises, or a response reports an
            ``error``), skip the async check (``True``) or STOP the run
            (``False``). How long to wait, and what a timeout means, is
            decided by the risk router (``async_wait_ms``, ``on_timeout``).
    """

    mode: Literal["shadow", "enforce"] = "enforce"
    sync_timeout_ms: float = 15.0
    sync_parallel: bool = True
    async_enabled: bool = True
    sync_fail_open: bool = False
    async_fail_open: bool = True


class DefaultDecisionPolicy(DecisionPolicy):
    """Priority-based resolution that combines redactions and effects."""

    def __init__(self, combine_effects: bool = True) -> None:
        """Resolve decisions, optionally merging the effects of all of them."""
        self._combine_effects = combine_effects

    @override
    def resolve(
        self,
        decisions: list[GuardrailDecision],
        event: GuardrailEvent,
        context_snapshot: ContextSnapshotV1,
    ) -> GuardrailDecision:
        if not decisions:
            return GuardrailDecision(
                action=GuardrailAction.ALLOW,
                rule_id="__default__",
                reason="No rules triggered",
            )

        priority = {
            GuardrailAction.STOP: 5,
            GuardrailAction.PAUSE: 4,
            GuardrailAction.RETRY: 3,
            GuardrailAction.REDACT: 2,
            GuardrailAction.ALLOW: 1,
        }
        sorted_decisions = sorted(
            decisions,
            key=lambda decision: (
                priority.get(decision.action, 0),
                decision.confidence or 0,
            ),
            reverse=True,
        )
        winner = sorted_decisions[0]

        if winner.action == GuardrailAction.REDACT:
            all_redactions: list[RedactionSpec] = []
            for decision in sorted_decisions:
                if decision.redactions:
                    all_redactions.extend(decision.redactions)
            if all_redactions:
                winner = GuardrailDecision(
                    action=GuardrailAction.REDACT,
                    rule_id=winner.rule_id,
                    reason=f"Combined {len(all_redactions)} redactions",
                    severity=winner.severity,
                    redactions=tuple(all_redactions),
                    effects=winner.effects,
                )

        if self._combine_effects:
            all_effects: set[str] = set()
            for decision in sorted_decisions:
                all_effects.update(decision.effects)
            if all_effects and all_effects != set(winner.effects):
                winner = winner.with_effects(*all_effects)

        return winner


@dataclass
class GuardrailGateway:
    """Unified gateway with extensibility hooks.

    DEEP rules run in the guard inbox. The gateway waits for them as long
    as the risk router says (``async_wait_ms``). A STOP that arrives after
    that wait is applied at the run's next guardrail check; late
    decisions of other kinds are dropped, because the action they
    reviewed has already happened.
    """

    registry: RuleRegistry
    guard_inbox: SteeringGuardInbox
    config: GatewayConfig = field(default_factory=GatewayConfig)
    decision_policy: DecisionPolicy = field(
        default_factory=DefaultDecisionPolicy
    )
    risk_router: RiskRouter = field(default_factory=DefaultRiskRouter)
    context_builder: ContextSnapshotBuilder = field(
        default_factory=ContextSnapshotBuilder
    )
    _late_ids: dict[str, set[str]] = field(
        default_factory=dict, init=False, repr=False
    )

    async def evaluate(
        self, ctx: GuardrailContext, event: GuardrailEvent
    ) -> GuardrailDecision:
        """Evaluate ``event`` against the rules; return the final decision."""
        context_snapshot = self.context_builder.build(event, ctx)
        late_stops = self._late_stops(event.run_id)
        if late_stops:
            return self._resolve(late_stops, event, context_snapshot, ctx)

        routing = self.risk_router.route(event, context_snapshot)
        if routing.skip_evaluation:
            return GuardrailDecision(
                action=GuardrailAction.ALLOW,
                rule_id="__skip__",
                reason="Skipped by risk router",
            )

        sync_decisions = await self._evaluate_sync(event, context_snapshot)
        sync_stops = [
            d for d in sync_decisions if d.action == GuardrailAction.STOP
        ]
        if sync_stops:
            return self._resolve(sync_stops, event, context_snapshot, ctx)

        async_decisions = await self._collect_async_decisions(
            event, context_snapshot, routing
        )
        all_decisions = sync_decisions + async_decisions
        if not all_decisions:
            return GuardrailDecision(
                action=GuardrailAction.ALLOW,
                rule_id="__default__",
                reason="No rules triggered",
            )
        return self._resolve(all_decisions, event, context_snapshot, ctx)

    def end_run(self, run_id: str) -> None:
        """Forget the late async responses of a finished run.

        The planner calls this when a run finishes, not when it pauses,
        so a response that arrives after the run ended is discarded.
        """
        self._late_ids.pop(run_id, None)
        self.guard_inbox.drain_responses(run_id)

    def _resolve(
        self,
        decisions: list[GuardrailDecision],
        event: GuardrailEvent,
        context_snapshot: ContextSnapshotV1,
        ctx: GuardrailContext,
    ) -> GuardrailDecision:
        final = self.decision_policy.resolve(
            decisions, event, context_snapshot
        )
        self._process_effects(final, ctx)
        return final

    def _late_stops(self, run_id: str) -> list[GuardrailDecision]:
        """Return the STOPs that arrived late for ``run_id``."""
        late_ids = self._late_ids.get(run_id)
        if not late_ids:
            return []
        stops: list[GuardrailDecision] = []
        for response in self.guard_inbox.drain_responses(run_id):
            if response.correlation_id not in late_ids:
                continue
            late_ids.discard(response.correlation_id)
            stops.extend(
                decision
                for decision in self._response_decisions(response, run_id)
                if decision.action == GuardrailAction.STOP
            )
        if not late_ids:
            self._late_ids.pop(run_id, None)
        return stops

    def _track_late(self, run_id: str, correlation_id: str) -> None:
        self._late_ids.setdefault(run_id, set()).add(correlation_id)

    async def _collect_async_decisions(
        self,
        event: GuardrailEvent,
        context_snapshot: ContextSnapshotV1,
        routing: RiskRoutingDecision,
    ) -> list[GuardrailDecision]:
        """Submit ``event`` to the inbox and wait as the router says."""
        if not self.config.async_enabled:
            return []
        try:
            correlation_id = await self._submit_async(
                event, context_snapshot, routing
            )
        except Exception as exc:
            return self._async_failure(
                "guardrail_async_submit_failed",
                event.run_id,
                f"{type(exc).__name__}: {exc}",
            )
        if routing.async_wait_ms <= 0:
            self._track_late(event.run_id, correlation_id)
            return []
        return await self._wait_for_async(
            event.run_id,
            correlation_id,
            timeout_ms=routing.async_wait_ms,
            on_timeout=routing.on_timeout,
        )

    def _response_decisions(
        self, response: SteeringGuardResponse, run_id: str
    ) -> list[GuardrailDecision]:
        """Return a response's decisions, or the async failure decision."""
        if response.error is not None:
            return self._async_failure(
                "guardrail_async_error", run_id, response.error
            )
        for decision in response.decisions:
            decision.was_sync = False
        return list(response.decisions)

    def _async_failure(
        self, log_event: str, run_id: str, detail: str
    ) -> list[GuardrailDecision]:
        """Apply ``async_fail_open`` to a guard inbox failure."""
        if self.config.async_fail_open:
            logger.warning(
                log_event, extra={"run_id": run_id, "error": detail}
            )
            return []
        return [
            GuardrailDecision(
                action=GuardrailAction.STOP,
                rule_id="__async_error__",
                reason="Async guardrail evaluation failed (fail-closed)",
                severity=GuardrailSeverity.CRITICAL,
                stop=StopSpec(
                    error_code="GUARDRAIL_ASYNC_ERROR",
                    internal_reason=detail,
                ),
            )
        ]

    async def _evaluate_sync(
        self,
        event: GuardrailEvent,
        context_snapshot: ContextSnapshotV1,
    ) -> list[GuardrailDecision]:
        rules = self.registry.get_sync_rules(event.event_type)
        if not rules:
            return []

        async def safe_eval(rule: GuardrailRule) -> GuardrailDecision | None:
            try:
                return await asyncio.wait_for(
                    rule.evaluate(event, context_snapshot),
                    timeout=self.config.sync_timeout_ms / 1000,
                )
            except TimeoutError as exc:
                if self.config.sync_fail_open:
                    return None
                return GuardrailDecision(
                    action=GuardrailAction.STOP,
                    rule_id=rule.rule_id,
                    reason=(
                        f"Sync guardrail '{rule.rule_id}' timed out "
                        "(fail-closed)"
                    ),
                    severity=GuardrailSeverity.CRITICAL,
                    stop=StopSpec(
                        error_code="GUARDRAIL_SYNC_TIMEOUT",
                        internal_reason=f"{type(exc).__name__}: {exc}",
                    ),
                )
            except Exception as exc:
                if self.config.sync_fail_open:
                    return None
                return GuardrailDecision(
                    action=GuardrailAction.STOP,
                    rule_id=rule.rule_id,
                    reason=(
                        f"Sync guardrail '{rule.rule_id}' errored "
                        "(fail-closed)"
                    ),
                    severity=GuardrailSeverity.CRITICAL,
                    stop=StopSpec(
                        error_code="GUARDRAIL_SYNC_ERROR",
                        internal_reason=f"{type(exc).__name__}: {exc}",
                    ),
                )

        if self.config.sync_parallel:
            results = await asyncio.gather(
                *[safe_eval(rule) for rule in rules]
            )
        else:
            results = [await safe_eval(rule) for rule in rules]

        return [decision for decision in results if decision is not None]

    async def _submit_async(
        self,
        event: GuardrailEvent,
        context_snapshot: ContextSnapshotV1,
        routing: RiskRoutingDecision,
    ) -> str:
        from penguiflow.steering.guard_inbox import SteeringGuardEvent

        steering_event = SteeringGuardEvent(
            event_type=event.event_type,
            run_id=event.run_id,
            payload={
                "text_content": event.text_content,
                "tool_name": event.tool_name,
                "tool_args": event.tool_args,
                **event.payload,
            },
            context_snapshot=context_snapshot.to_dict(),
            required_rules=routing.required_async_rules,
        )
        return await self.guard_inbox.submit(steering_event)

    async def _wait_for_async(
        self,
        run_id: str,
        correlation_id: str,
        *,
        timeout_ms: float,
        on_timeout: Literal["allow", "pause", "stop"],
    ) -> list[GuardrailDecision]:
        try:
            response = await self.guard_inbox.await_response(
                correlation_id,
                timeout_s=timeout_ms / 1000,
            )
        except TimeoutError:
            self._track_late(run_id, correlation_id)
            return _timeout_decisions(on_timeout)
        except Exception as exc:
            return self._async_failure(
                "guardrail_async_error",
                run_id,
                f"{type(exc).__name__}: {exc}",
            )
        return self._response_decisions(response, run_id)

    def _process_effects(
        self, decision: GuardrailDecision, ctx: GuardrailContext
    ) -> None:
        for effect in decision.effects:
            if effect == "increment_strike":
                category = decision.rule_id.split("-")[0]
                ctx.strike_counts[category] = (
                    ctx.strike_counts.get(category, 0) + 1
                )
            elif effect == "flag_trajectory" or effect == "emit_alert":
                pass


def _timeout_decisions(
    on_timeout: Literal["allow", "pause", "stop"],
) -> list[GuardrailDecision]:
    """Return the decision the router chose for an async timeout."""
    if on_timeout == "stop":
        return [
            GuardrailDecision(
                action=GuardrailAction.STOP,
                rule_id="__timeout__",
                reason="Async evaluation timed out (fail-closed)",
                stop=StopSpec(error_code="GUARDRAIL_TIMEOUT"),
            )
        ]
    if on_timeout == "pause":
        return [
            GuardrailDecision(
                action=GuardrailAction.PAUSE,
                rule_id="__timeout__",
                reason="Async evaluation timed out",
                pause=PauseSpec(prompt="Safety check timed out"),
            )
        ]
    return []


__all__ = ["DefaultDecisionPolicy", "GatewayConfig", "GuardrailGateway"]
