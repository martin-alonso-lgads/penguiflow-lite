"""Turn a guardrail STOP or PAUSE decision into the run's outcome.

Tool-side and LLM-side checks share these helpers, so a decision has the
same effect wherever it is made: a STOP ends the run with
``reason="no_path"``, and a PAUSE pauses it with
``reason="approval_required"``. On a planner built with
``pause_enabled=False``, a PAUSE ends the run like a STOP.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .pause import _PlannerPauseError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .guardrails import GuardrailDecision
    from .models import PlannerPause
    from .react import ReactPlanner
    from .trajectory import Trajectory

GUARDRAIL_STOP_KEY = "guardrail_stop"
"""Trajectory metadata key of the STOP payload that ends the run."""

GUARDRAIL_PAUSE_KEY = "guardrail_pause"
"""Trajectory metadata key of the PAUSE payload that pauses the run."""

BLOCKING_ACTIONS = frozenset({"STOP", "PAUSE"})
"""Guardrail actions that end or pause the run."""

_PAUSE_DISABLED_MESSAGE = (
    "This request needs approval, and this agent cannot pause for it."
)


def pauses_run(planner: ReactPlanner, decision: GuardrailDecision) -> bool:
    """Whether ``decision`` pauses the run rather than ending it."""
    return decision.action.value == "PAUSE" and planner.pause_enabled


def stop_payload(
    planner: ReactPlanner, decision: GuardrailDecision
) -> dict[str, Any]:
    """Return the payload of the STOP that ``decision`` ends the run with.

    A PAUSE on a planner that cannot pause becomes a STOP whose payload has
    ``requested_action="PAUSE"`` and ``pause_disabled=True``.
    """
    payload = planner.guardrail_decision_payload(decision)
    if decision.action.value != "PAUSE":
        return payload
    prompt = decision.pause.prompt if decision.pause is not None else ""
    payload.update(
        action="STOP",
        requested_action="PAUSE",
        pause_disabled=True,
        stop={
            "error_code": "GUARDRAIL_PAUSE_DISABLED",
            "user_message": prompt or _PAUSE_DISABLED_MESSAGE,
            "internal_reason": decision.reason,
        },
    )
    return payload


async def guardrail_pause(
    planner: ReactPlanner,
    trajectory: Trajectory,
    guardrail: Mapping[str, Any],
) -> PlannerPause:
    """Pause the run for a guardrail PAUSE and return the recorded pause.

    ``pause_from_context`` raises to unwind tool code; guardrail checks
    run outside tools, so the signal is caught here and the recorded
    pause is returned instead.

    Args:
        planner: The planner running the trajectory.
        trajectory: The run to pause.
        guardrail: The PAUSE decision's payload, as
            ``planner.guardrail_decision_payload`` returns it.

    Returns:
        The pause, whose payload is ``{"guardrail": guardrail}``.
    """
    payload = {"guardrail": dict(guardrail)}
    try:
        return await planner.pause_from_context(
            "approval_required", payload, trajectory
        )
    except _PlannerPauseError as signal:
        return signal.pause


__all__ = [
    "BLOCKING_ACTIONS",
    "GUARDRAIL_PAUSE_KEY",
    "GUARDRAIL_STOP_KEY",
    "guardrail_pause",
    "pauses_run",
    "stop_payload",
]
