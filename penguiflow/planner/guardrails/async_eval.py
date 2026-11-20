"""Async evaluator for deep guardrail rules."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from .context import ContextSnapshotV1, GuardrailEvent

if TYPE_CHECKING:
    from penguiflow.steering.guard_inbox import SteeringGuardEvent

    from .models import GuardrailDecision
    from .protocols import GuardrailRule
    from .registry import RuleRegistry


logger = logging.getLogger(__name__)


class AsyncRuleEvaluationError(Exception):
    """One or more DEEP rules raised while evaluating an event.

    Attributes:
        decisions: The decisions of the rules that did not raise.
        errors: The exceptions, keyed by rule id.
    """

    def __init__(
        self,
        decisions: list[GuardrailDecision],
        errors: dict[str, BaseException],
    ) -> None:
        """Keep the good decisions and the rules' errors."""
        detail = "; ".join(
            f"{rule_id}: {type(exc).__name__}: {exc}"
            for rule_id, exc in errors.items()
        )
        super().__init__(f"Async guardrail rules failed: {detail}")
        self.decisions = decisions
        self.errors = errors


class AsyncRuleEvaluator:
    """Evaluates deep (async) rules for a submitted event."""

    def __init__(self, registry: RuleRegistry) -> None:
        """Evaluate the async rules of ``registry``."""
        self._registry = registry

    async def evaluate(
        self, event: SteeringGuardEvent
    ) -> list[GuardrailDecision]:
        """Run the event's async rules concurrently; return their decisions.

        Every registered DEEP rule for the event type runs.
        ``event.required_rules`` names rules the router wants run; one that
        is not registered is logged.

        Raises:
            AsyncRuleEvaluationError: If a rule raised. It carries the other
                rules' decisions, so the gateway can apply
                ``GatewayConfig.async_fail_open`` to the failure.
        """
        rules = self._registry.get_async_rules(event.event_type)
        self._warn_missing_required(event, rules)
        if not rules:
            return []

        context_snapshot = (
            ContextSnapshotV1(**event.context_snapshot)
            if event.context_snapshot
            else ContextSnapshotV1()
        )
        guardrail_event = GuardrailEvent(
            event_type=event.event_type,
            run_id=event.run_id,
            text_content=event.payload.get("text_content"),
            tool_name=event.payload.get("tool_name"),
            tool_args=event.payload.get("tool_args"),
            payload=event.payload,
        )

        results = await asyncio.gather(
            *[
                rule.evaluate(guardrail_event, context_snapshot)
                for rule in rules
            ],
            return_exceptions=True,
        )
        decisions: list[GuardrailDecision] = []
        errors: dict[str, BaseException] = {}
        for rule, result in zip(rules, results, strict=True):
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                errors[rule.rule_id] = result
            elif result is not None:
                result.was_sync = False
                decisions.append(result)
        if errors:
            raise AsyncRuleEvaluationError(decisions, errors)
        return decisions

    @staticmethod
    def _warn_missing_required(
        event: SteeringGuardEvent, rules: list[GuardrailRule]
    ) -> None:
        missing = event.required_rules - {rule.rule_id for rule in rules}
        if missing:
            logger.warning(
                "guardrail_required_rule_missing",
                extra={
                    "event_type": event.event_type,
                    "rule_ids": sorted(missing),
                },
            )


__all__ = ["AsyncRuleEvaluationError", "AsyncRuleEvaluator"]
