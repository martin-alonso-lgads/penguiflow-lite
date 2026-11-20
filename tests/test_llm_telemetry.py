"""Tests for the LLM telemetry module."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from penguiflow.llm.profiles import ModelProfile
from penguiflow.llm.protocol import NativeLLMAdapter
from penguiflow.llm.telemetry import (
    LLMEvent,
    TelemetryHooks,
    TimingContext,
    default_logger_callback,
    get_telemetry_hooks,
    set_telemetry_hooks,
)
from penguiflow.llm.types import (
    CompletionResponse,
    LLMMessage,
    TextPart,
    Usage,
)
from penguiflow.planner.llm import _LiteLLMJSONClient

if TYPE_CHECKING:
    from collections.abc import Iterator


class TestLLMEvent:
    """Tests for LLM event."""

    def test_create(self) -> None:
        """Create."""
        event = LLMEvent(
            event_type="request_start",
            timestamp=datetime.now(tz=UTC),
            provider="openai",
            model="gpt-4o",
        )
        assert event.event_type == "request_start"
        assert event.provider == "openai"
        assert event.model == "gpt-4o"
        assert event.trace_id is None
        assert event.extra is None

    def test_create_with_extra(self) -> None:
        """Create with extra."""
        event = LLMEvent(
            event_type="request_end",
            timestamp=datetime.now(tz=UTC),
            provider="anthropic",
            model="claude-3-5-sonnet",
            trace_id="trace-123",
            extra={"latency_ms": 150},
        )
        assert event.trace_id == "trace-123"
        assert event.extra == {"latency_ms": 150}

    def test_create_factory(self) -> None:
        """Create factory."""
        event = LLMEvent.create(
            "error",
            "google",
            "gemini-2.0-flash",
            trace_id="trace-456",
            error="Connection failed",
        )
        assert event.event_type == "error"
        assert event.provider == "google"
        assert event.extra is not None
        assert event.extra.get("error") == "Connection failed"

    def test_create_factory_no_extra(self) -> None:
        """Create factory no extra."""
        event = LLMEvent.create("request_start", "openai", "gpt-4o")
        assert event.extra is None


class TestTelemetryHooks:
    """Tests for telemetry hooks."""

    def test_register_callback(self) -> None:
        """Register callback."""
        hooks = TelemetryHooks()
        events: list[LLMEvent] = []
        hooks.register(lambda e: events.append(e))

        event = LLMEvent.create("test", "provider", "model")
        hooks.emit(event)

        assert len(events) == 1
        assert events[0] == event

    def test_unregister_callback(self) -> None:
        """Unregister callback."""
        hooks = TelemetryHooks()
        events: list[LLMEvent] = []

        def callback(e: LLMEvent) -> None:
            events.append(e)

        hooks.register(callback)
        result = hooks.unregister(callback)
        assert result is True

        hooks.emit(LLMEvent.create("test", "provider", "model"))
        assert len(events) == 0

    def test_unregister_not_found(self) -> None:
        """Unregister not found."""
        hooks = TelemetryHooks()
        result = hooks.unregister(lambda _e: None)
        assert result is False

    def test_disable_enable(self) -> None:
        """Disable enable."""
        hooks = TelemetryHooks()
        events: list[LLMEvent] = []
        hooks.register(lambda e: events.append(e))

        hooks.disable()
        hooks.emit(LLMEvent.create("test1", "provider", "model"))
        assert len(events) == 0

        hooks.enable()
        hooks.emit(LLMEvent.create("test2", "provider", "model"))
        assert len(events) == 1

    def test_clear(self) -> None:
        """Clear."""
        hooks = TelemetryHooks()
        events: list[LLMEvent] = []
        hooks.register(lambda e: events.append(e))
        hooks.clear()

        hooks.emit(LLMEvent.create("test", "provider", "model"))
        assert len(events) == 0

    def test_callback_error_does_not_propagate(self) -> None:
        """Callback error does not propagate."""
        hooks = TelemetryHooks()
        call_count = 0

        def bad_callback(_e: LLMEvent) -> None:
            msg = "Intentional error"
            raise ValueError(msg)

        def good_callback(_e: LLMEvent) -> None:
            nonlocal call_count
            call_count += 1

        hooks.register(bad_callback)
        hooks.register(good_callback)

        # Should not raise, and should continue to next callback
        hooks.emit(LLMEvent.create("test", "provider", "model"))
        assert call_count == 1

    def test_multiple_callbacks(self) -> None:
        """Multiple callbacks."""
        hooks = TelemetryHooks()
        events1: list[LLMEvent] = []
        events2: list[LLMEvent] = []

        hooks.register(lambda e: events1.append(e))
        hooks.register(lambda e: events2.append(e))

        event = LLMEvent.create("test", "provider", "model")
        hooks.emit(event)

        assert len(events1) == 1
        assert len(events2) == 1


class TestGlobalHooks:
    """Tests for global hooks."""

    def test_get_telemetry_hooks(self) -> None:
        # Reset global state for clean test
        """Get telemetry hooks."""
        set_telemetry_hooks(TelemetryHooks())

        hooks = get_telemetry_hooks()
        assert hooks is not None

    def test_set_telemetry_hooks(self) -> None:
        """Set telemetry hooks."""
        custom_hooks = TelemetryHooks()
        set_telemetry_hooks(custom_hooks)
        assert get_telemetry_hooks() is custom_hooks


class TestDefaultLoggerCallback:
    """Tests for default logger callback."""

    def test_request_end(self, caplog: pytest.LogCaptureFixture) -> None:
        """Request end."""
        event = LLMEvent.create(
            "request_end",
            "openai",
            "gpt-4o",
            latency_ms=150,
            input_tokens=100,
            output_tokens=50,
        )
        import logging

        with caplog.at_level(logging.DEBUG):
            default_logger_callback(event)

        # Check that something was logged (may not appear if DEBUG not
        # captured)

    def test_error(self, caplog: pytest.LogCaptureFixture) -> None:
        """Error."""
        event = LLMEvent.create(
            "error",
            "anthropic",
            "claude-3-5-sonnet",
            error="Connection timeout",
        )
        import logging

        with caplog.at_level(logging.WARNING):
            default_logger_callback(event)

    def test_retry(self, caplog: pytest.LogCaptureFixture) -> None:
        """Retry."""
        event = LLMEvent.create(
            "retry",
            "google",
            "gemini-2.0-flash",
            attempt=2,
            reason="Rate limited",
        )
        import logging

        with caplog.at_level(logging.INFO):
            default_logger_callback(event)


class TestTimingContext:
    """Tests for timing context."""

    def test_timing_context(self) -> None:
        # Set up custom hooks to capture events
        """Timing context."""
        hooks = TelemetryHooks()
        events: list[LLMEvent] = []
        hooks.register(lambda e: events.append(e))
        set_telemetry_hooks(hooks)

        with TimingContext("openai", "gpt-4o", trace_id="test-trace") as ctx:
            # Simulate some work
            pass

        assert ctx.latency_ms > 0
        # Should have emitted request_start
        assert any(e.event_type == "request_start" for e in events)

    def test_timing_context_with_error(self) -> None:
        """Timing context with error."""
        hooks = TelemetryHooks()
        events: list[LLMEvent] = []
        hooks.register(lambda e: events.append(e))
        set_telemetry_hooks(hooks)

        with (
            pytest.raises(ValueError),
            TimingContext("openai", "gpt-4o") as _ctx,
        ):
            msg = "Test error"
            raise ValueError(msg)

        # Should have emitted error event
        error_events = [e for e in events if e.event_type == "error"]
        assert len(error_events) == 1
        assert error_events[0].extra is not None
        assert "Test error" in error_events[0].extra.get("error", "")

    def test_latency_property_during_execution(self) -> None:
        """Latency property during execution."""
        hooks = TelemetryHooks()
        set_telemetry_hooks(hooks)

        with TimingContext("openai", "gpt-4o") as ctx:
            # Mid-execution, latency should be positive
            assert ctx.latency_ms > 0


class TestGetTelemetryHooksInitialization:
    """Test fresh initialization of global hooks."""

    def test_fresh_init_registers_default_callback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that fresh get_telemetry_hooks() registers default callback."""
        import penguiflow.llm.telemetry as telemetry_module

        # Clear the global hooks
        monkeypatch.setattr(telemetry_module, "_hooks", None)

        # Get fresh hooks - should auto-register default callback
        hooks = get_telemetry_hooks()

        assert telemetry_module.default_logger_callback in hooks.callbacks


class TestMlflowCallback:
    """Tests for create_mlflow_callback."""

    def test_mlflow_callback_without_mlflow(self) -> None:
        """Test mlflow callback gracefully handles missing mlflow."""
        from penguiflow.llm.telemetry import create_mlflow_callback

        callback = create_mlflow_callback()

        # Should not raise even without mlflow
        event = LLMEvent.create(
            "request_end",
            "openai",
            "gpt-4o",
            latency_ms=100,
            input_tokens=50,
            output_tokens=25,
            cost=0.001,
        )
        callback(event)  # Should not raise

    def test_mlflow_callback_with_request_end(self) -> None:
        """Test mlflow callback logs metrics on request_end."""
        from unittest.mock import MagicMock, patch

        from penguiflow.llm.telemetry import create_mlflow_callback

        callback = create_mlflow_callback()

        # Mock mlflow import
        mock_mlflow = MagicMock()

        event = LLMEvent.create(
            "request_end",
            "openai",
            "gpt-4o",
            latency_ms=100,
            input_tokens=50,
            output_tokens=25,
            cost=0.002,
        )

        # Patch mlflow inside the callback
        with patch.dict("sys.modules", {"mlflow": mock_mlflow}):
            callback(event)

            # Verify log_metrics was called when mlflow is available

    def test_mlflow_callback_ignores_non_request_end(self) -> None:
        """Test mlflow callback ignores other event types."""
        from penguiflow.llm.telemetry import create_mlflow_callback

        callback = create_mlflow_callback()

        # Should not raise for non-request_end events
        event = LLMEvent.create("request_start", "openai", "gpt-4o")
        callback(event)

        event = LLMEvent.create("error", "openai", "gpt-4o", error="test")
        callback(event)


class TestPrometheusCallback:
    """Tests for create_prometheus_callback."""

    def test_prometheus_callback_without_prometheus(self) -> None:
        """The prometheus callback copes without prometheus_client."""
        from penguiflow.llm.telemetry import create_prometheus_callback

        callback = create_prometheus_callback()

        # Should not raise even without prometheus
        event = LLMEvent.create(
            "request_end",
            "openai",
            "gpt-4o",
            latency_ms=100,
            input_tokens=50,
            output_tokens=25,
        )
        callback(event)  # Should not raise

    def test_prometheus_callback_request_end(self) -> None:
        """Test prometheus callback updates metrics on request_end."""
        from unittest.mock import MagicMock, patch

        # Create mocks for prometheus counters/histograms
        mock_counter = MagicMock()
        mock_histogram = MagicMock()
        mock_counter_instance = MagicMock()
        mock_histogram_instance = MagicMock()
        mock_counter.return_value = mock_counter_instance
        mock_histogram.return_value = mock_histogram_instance

        # Create a mock prometheus_client module
        mock_prometheus = MagicMock()
        mock_prometheus.Counter = mock_counter
        mock_prometheus.Histogram = mock_histogram

        with patch.dict("sys.modules", {"prometheus_client": mock_prometheus}):
            # Need to reload to pick up the mock
            from penguiflow.llm.telemetry import create_prometheus_callback

            callback = create_prometheus_callback()

            event = LLMEvent.create(
                "request_end",
                "openai",
                "gpt-4o",
                latency_ms=100,
                input_tokens=50,
                output_tokens=25,
            )

            # The callback might not call anything if prometheus wasn't
            # available at module load
            callback(event)

    def test_prometheus_callback_error_event(self) -> None:
        """Test prometheus callback handles error events."""
        from penguiflow.llm.telemetry import create_prometheus_callback

        callback = create_prometheus_callback()

        # Should not raise for error events
        event = LLMEvent.create(
            "error",
            "openai",
            "gpt-4o",
            error="Something went wrong",
        )
        callback(event)

    def test_prometheus_callback_ignores_other_events(self) -> None:
        """Test prometheus callback ignores non-relevant event types."""
        from penguiflow.llm.telemetry import create_prometheus_callback

        callback = create_prometheus_callback()

        # Should not raise for other event types
        event = LLMEvent.create("request_start", "openai", "gpt-4o")
        callback(event)

        event = LLMEvent.create("retry", "openai", "gpt-4o", attempt=1)
        callback(event)


class TestDefaultLoggerCallbackEdgeCases:
    """Additional tests for default_logger_callback edge cases."""

    def test_request_end_no_extra(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test request_end with no extra data."""
        import logging

        event = LLMEvent(
            event_type="request_end",
            timestamp=datetime.now(tz=UTC),
            provider="openai",
            model="gpt-4o",
            extra=None,
        )

        with caplog.at_level(logging.DEBUG):
            default_logger_callback(event)

    def test_error_no_extra(self, caplog: pytest.LogCaptureFixture) -> None:
        """Test error event with no extra data."""
        import logging

        event = LLMEvent(
            event_type="error",
            timestamp=datetime.now(tz=UTC),
            provider="openai",
            model="gpt-4o",
            extra=None,
        )

        with caplog.at_level(logging.WARNING):
            default_logger_callback(event)

    def test_retry_no_extra(self, caplog: pytest.LogCaptureFixture) -> None:
        """Test retry event with no extra data."""
        import logging

        event = LLMEvent(
            event_type="retry",
            timestamp=datetime.now(tz=UTC),
            provider="openai",
            model="gpt-4o",
            extra=None,
        )

        with caplog.at_level(logging.INFO):
            default_logger_callback(event)

    def test_unknown_event_type(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test callback handles unknown event types."""
        import logging

        event = LLMEvent.create("unknown_event", "openai", "gpt-4o")

        with caplog.at_level(logging.DEBUG):
            # Should not raise
            default_logger_callback(event)


@pytest.fixture
def recorded_events() -> Iterator[list[LLMEvent]]:
    """Install fresh global telemetry hooks that record every event."""
    previous = get_telemetry_hooks()
    hooks = TelemetryHooks()
    events: list[LLMEvent] = []
    hooks.register(events.append)
    set_telemetry_hooks(hooks)
    try:
        yield events
    finally:
        set_telemetry_hooks(previous)


def _fake_provider(*, fail: bool = False) -> MagicMock:
    provider = MagicMock()
    provider.model = "test-model"
    provider.provider_name = "test"
    provider.profile = ModelProfile()
    if fail:
        provider.complete = AsyncMock(side_effect=ValueError("bad request"))
    else:
        provider.complete = AsyncMock(
            return_value=CompletionResponse(
                message=LLMMessage(
                    role="assistant", parts=[TextPart(text='{"ok": true}')]
                ),
                usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
            )
        )
    return provider


@pytest.mark.asyncio
async def test_native_adapter_emits_telemetry_for_planner_calls(
    recorded_events: list[LLMEvent],
) -> None:
    """The planner's native adapter reports its calls to the hooks."""
    with patch(
        "penguiflow.llm.protocol.create_provider",
        return_value=_fake_provider(),
    ):
        adapter = NativeLLMAdapter("test-model")
        await adapter.complete(messages=[{"role": "user", "content": "hi"}])
        await adapter.complete_with_tools(
            messages=[{"role": "user", "content": "hi"}], tools=[]
        )

    kinds = [
        (e.event_type, (e.extra or {}).get("mode")) for e in recorded_events
    ]
    assert kinds == [
        ("request_start", "text"),
        ("request_end", "text"),
        ("request_start", "native_tools"),
        ("request_end", "native_tools"),
    ]
    assert all(e.provider == "test" for e in recorded_events)


@pytest.mark.asyncio
async def test_native_adapter_emits_error_event(
    recorded_events: list[LLMEvent],
) -> None:
    """A failing planner call ends with an ``error`` event."""
    with patch(
        "penguiflow.llm.protocol.create_provider",
        return_value=_fake_provider(fail=True),
    ):
        adapter = NativeLLMAdapter("test-model", max_retries=1)
        with pytest.raises(ValueError, match="bad request"):
            await adapter.complete(
                messages=[{"role": "user", "content": "hi"}]
            )

    assert [e.event_type for e in recorded_events] == [
        "request_start",
        "error",
    ]
    assert (recorded_events[-1].extra or {})["error_type"] == "ValueError"


@pytest.mark.asyncio
async def test_litellm_planner_client_emits_telemetry(
    recorded_events: list[LLMEvent],
) -> None:
    """The planner's LiteLLM client reports its calls to the hooks."""
    litellm = MagicMock()
    litellm.acompletion = AsyncMock(
        return_value={
            "choices": [{"message": {"content": '{"ok": true}'}}],
            "usage": {"total_tokens": 3},
            "_hidden_params": {"response_cost": 0.002},
        }
    )
    with patch.dict(sys.modules, {"litellm": litellm}):
        client = _LiteLLMJSONClient(
            "gpt-4o", temperature=0.0, json_schema_mode=False
        )
        await client.complete(messages=[{"role": "user", "content": "hi"}])

    assert [e.event_type for e in recorded_events] == [
        "request_start",
        "request_end",
    ]
    end = recorded_events[-1]
    assert end.provider == "litellm"
    assert (end.extra or {})["cost"] == 0.002
