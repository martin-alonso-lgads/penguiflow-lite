"""Tests for context merge."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from typing_extensions import override

from penguiflow.planner import ReactPlanner, Trajectory
from penguiflow.planner.models import JSONLLMClient
from penguiflow.sessions import ContextPatch, MergeStrategy, StreamingSession

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence


@pytest.mark.asyncio
async def test_human_gated_merge_creates_pending_patch() -> None:
    """Human gated merge creates pending patch."""
    session = StreamingSession("session-merge")
    session.update_context(llm_context={"state": "v1"})
    patch = ContextPatch(
        task_id="task-1",
        digest=["summary"],
        source_context_version=0,
        source_context_hash="stale",
    )
    patch_id = await session.apply_context_patch(
        patch=patch, strategy=MergeStrategy.HUMAN_GATED
    )
    assert patch_id is not None
    pending = session.pending_patches[patch_id]
    assert pending.patch.context_diverged is True


@pytest.mark.asyncio
async def test_apply_pending_patch_updates_context() -> None:
    """Apply pending patch updates context."""
    session = StreamingSession("session-merge-apply")
    patch = ContextPatch(
        task_id="task-2",
        digest=["summary"],
    )
    patch_id = await session.apply_context_patch(
        patch=patch, strategy=MergeStrategy.HUMAN_GATED
    )
    assert patch_id is not None
    applied = await session.apply_pending_patch(patch_id=patch_id)
    assert applied is True
    background = session.get_background_results()
    assert "task-2" in background
    llm_context, _tool = session.get_context()
    assert "background_results" not in llm_context


@pytest.mark.asyncio
async def test_mark_background_consumed_removes_entries() -> None:
    """Mark background consumed removes entries."""
    session = StreamingSession("session-merge-consume")
    append_patch = ContextPatch(task_id="task-append", digest=["summary"])
    replace_patch = ContextPatch(task_id="task-replace", digest=["latest"])
    await session.apply_context_patch(
        patch=append_patch, strategy=MergeStrategy.APPEND
    )
    await session.apply_context_patch(
        patch=replace_patch, strategy=MergeStrategy.REPLACE
    )

    background = session.get_background_results()
    assert set(background.keys()) == {"task-append", "task-replace"}
    llm_context, _tool = session.get_context()
    assert "background_results" not in llm_context
    assert "background_result" not in llm_context

    removed = await session.mark_background_consumed(
        task_ids=["task-append", "task-replace"]
    )
    assert removed == 2
    background = session.get_background_results()
    assert not background
    llm_context, _tool = session.get_context()
    assert "background_results" not in llm_context
    assert "background_result" not in llm_context
    removed_again = await session.mark_background_consumed(
        task_ids=["task-append"]
    )
    assert removed_again == 0


@pytest.mark.asyncio
async def test_approved_patch_keeps_payload_and_group() -> None:
    """Approving a gated patch keeps its payload and group id."""
    session = StreamingSession("session-merge-payload")
    patch_id = await session.apply_context_patch(
        patch=ContextPatch(task_id="task-3", digest=["three rows"]),
        strategy=MergeStrategy.HUMAN_GATED,
        payload={"rows": 3},
        group_id="group-1",
    )
    assert patch_id is not None

    assert await session.apply_pending_patch(patch_id=patch_id) is True

    result = session.get_background_results()["task-3"]
    assert result.payload == {"rows": 3}
    assert result.group_id == "group-1"


@pytest.mark.asyncio
async def test_merged_background_results_reach_the_prompt() -> None:
    """A merged result's summary and facts are shown to the LLM."""
    planner = ReactPlanner(
        llm_client=_NoCallsClient(), catalog=[], max_iters=1
    )
    trajectory = Trajectory(
        query="what did the job find?",
        llm_context={
            "background_results": [
                {
                    "task_id": "task-9",
                    "summary": "found 42 invoices",
                    "facts": {"invoice_count": 42},
                    "payload": {"rows": ["huge"] * 3},
                }
            ]
        },
    )

    messages = await planner.build_messages(trajectory)

    block = next(
        m["content"]
        for m in messages
        if "<read_only_background_results>" in str(m["content"])
    )
    assert "found 42 invoices" in block
    assert '"invoice_count": 42' in block
    assert "huge" not in block
    assert "background_results" not in str(messages[-1]["content"])


class _NoCallsClient(JSONLLMClient):
    """LLM client double for planners whose LLM is never called."""

    @override
    async def complete(
        self,
        *,
        messages: Sequence[Mapping[str, Any]],
        response_format: Mapping[str, Any] | None = None,
        stream: bool = False,
        on_stream_chunk: Callable[[str, bool], None] | None = None,
    ) -> tuple[str, float]:
        msg = f"unexpected LLM call with {len(messages)} messages"
        raise AssertionError(msg)
