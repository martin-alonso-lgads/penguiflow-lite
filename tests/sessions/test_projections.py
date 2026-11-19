"""Tests for mapping planner stream chunks to session updates."""

from __future__ import annotations

from typing import Any

import pytest

from penguiflow.planner import PlannerEvent
from penguiflow.sessions.models import UpdateType
from penguiflow.sessions.projections import PlannerEventProjector


def _project(event_type: str, extra: dict[str, Any]) -> tuple[UpdateType, Any]:
    projector = PlannerEventProjector(session_id="s1", task_id="t1")
    event = PlannerEvent(
        event_type=event_type, ts=0.0, trajectory_step=0, extra=extra
    )
    [update] = projector.project(event)
    return update.update_type, update.content


@pytest.mark.parametrize(
    ("event_type", "extra", "expected_type", "expected_channel"),
    [
        (
            "stream_chunk",
            {"text": "hi", "meta": {"channel": "answer"}},
            UpdateType.RESULT,
            "answer",
        ),
        (
            "stream_chunk",
            {"text": "hi", "meta": {"channel": "thinking"}},
            UpdateType.THINKING,
            "thinking",
        ),
        ("stream_chunk", {"text": "hi"}, UpdateType.THINKING, "thinking"),
        (
            "llm_stream_chunk",
            {"text": "hi", "channel": "answer"},
            UpdateType.RESULT,
            "answer",
        ),
        (
            "llm_stream_chunk",
            {"text": "hi", "channel": "revision"},
            UpdateType.RESULT,
            "revision",
        ),
    ],
)
def test_stream_chunk_channel_selects_update_type(
    event_type: str,
    extra: dict[str, Any],
    expected_type: UpdateType,
    expected_channel: str,
) -> None:
    """Tool chunks read the channel from meta, LLM chunks from the top."""
    update_type, content = _project(event_type, extra)

    assert update_type is expected_type
    assert content["channel"] == expected_channel
    assert content["text"] == "hi"


def test_top_level_channel_wins_over_meta() -> None:
    """A top-level channel takes precedence over the meta channel."""
    update_type, content = _project(
        "stream_chunk",
        {"text": "x", "channel": "thinking", "meta": {"channel": "answer"}},
    )

    assert update_type is UpdateType.THINKING
    assert content["channel"] == "thinking"
