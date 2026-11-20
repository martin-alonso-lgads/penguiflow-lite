"""Async guardrail inbox implementations."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from typing_extensions import override

if TYPE_CHECKING:
    from penguiflow.planner.guardrails.async_eval import AsyncRuleEvaluator
    from penguiflow.planner.guardrails.models import GuardrailDecision


class SteeringGuardInbox(ABC):
    """Interface for async guardrail evaluation."""

    @abstractmethod
    async def submit(self, event: SteeringGuardEvent) -> str:
        """Submit event for async evaluation. Returns correlation_id."""
        raise NotImplementedError

    @abstractmethod
    async def await_response(
        self, correlation_id: str, timeout_s: float
    ) -> SteeringGuardResponse:
        """Wait for response. Raises TimeoutError if exceeded.

        A timeout must not discard the evaluation: its response stays
        available to ``drain_responses`` once it completes.
        """
        raise NotImplementedError

    @abstractmethod
    def drain_responses(self, run_id: str) -> list[SteeringGuardResponse]:
        """Return and forget the completed responses of ``run_id``.

        The gateway calls this to collect decisions that arrived after it
        stopped waiting, and when a run finishes. Only responses for
        events submitted with this ``run_id`` are returned.
        """
        raise NotImplementedError


@dataclass(slots=True)
class SteeringGuardEvent:
    """Event submitted for async evaluation."""

    event_id: str = field(default_factory=lambda: uuid4().hex)
    correlation_id: str = field(default_factory=lambda: uuid4().hex)
    event_type: str = ""
    run_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    context_snapshot: dict[str, Any] = field(default_factory=dict)
    required_rules: frozenset[str] = frozenset()


@dataclass(slots=True)
class SteeringGuardResponse:
    """Response from async evaluation."""

    correlation_id: str
    decisions: list[GuardrailDecision] = field(default_factory=list)
    error: str | None = None


class InMemoryGuardInbox(SteeringGuardInbox):
    """In-process async evaluation."""

    def __init__(self, evaluator: AsyncRuleEvaluator) -> None:
        """Create an inbox that evaluates events with ``evaluator``."""
        self._evaluator = evaluator
        self._futures: dict[str, asyncio.Future[SteeringGuardResponse]] = {}
        self._run_ids: dict[str, str] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    @override
    async def submit(self, event: SteeringGuardEvent) -> str:
        future: asyncio.Future[SteeringGuardResponse] = (
            asyncio.get_running_loop().create_future()
        )
        self._futures[event.correlation_id] = future
        self._run_ids[event.correlation_id] = event.run_id
        task = asyncio.create_task(self._evaluate(event, future))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return event.correlation_id

    async def _evaluate(
        self,
        event: SteeringGuardEvent,
        future: asyncio.Future[SteeringGuardResponse],
    ) -> None:
        from penguiflow.planner.guardrails.async_eval import (
            AsyncRuleEvaluationError,
        )

        try:
            decisions = await self._evaluator.evaluate(event)
            response = SteeringGuardResponse(
                correlation_id=event.correlation_id,
                decisions=decisions,
            )
        except AsyncRuleEvaluationError as exc:
            response = SteeringGuardResponse(
                correlation_id=event.correlation_id,
                decisions=exc.decisions,
                error=str(exc),
            )
        except Exception as exc:
            response = SteeringGuardResponse(
                correlation_id=event.correlation_id,
                error=f"{type(exc).__name__}: {exc}",
            )

        if not future.done():
            future.set_result(response)

    @override
    async def await_response(
        self, correlation_id: str, timeout_s: float
    ) -> SteeringGuardResponse:
        future = self._futures.get(correlation_id)
        if future is None:
            msg = f"Unknown correlation_id: {correlation_id}"
            raise KeyError(msg)
        # Shield the future so a timeout leaves the evaluation running and
        # its late response drainable.
        return await asyncio.wait_for(asyncio.shield(future), timeout_s)

    @override
    def drain_responses(self, run_id: str) -> list[SteeringGuardResponse]:
        done = [
            correlation_id
            for correlation_id, future in self._futures.items()
            if future.done() and self._run_ids.get(correlation_id) == run_id
        ]
        responses = [self._futures[cid].result() for cid in done]
        for correlation_id in done:
            self._futures.pop(correlation_id, None)
            self._run_ids.pop(correlation_id, None)
        return responses
