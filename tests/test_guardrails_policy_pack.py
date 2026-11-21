"""Tests for guardrails policy pack."""

from __future__ import annotations

import textwrap
from pathlib import Path

from examples.guardrails.huggingface.rule import HuggingFaceJailbreakRule
from penguiflow.planner.guardrails import (
    AsyncRuleEvaluator,
    ContextSnapshotBuilder,
    ContextSnapshotV1,
    DefaultRiskRouter,
    GatewayConfig,
    GuardrailEvent,
    GuardrailGateway,
    InjectionPatternRule,
    RuleRegistry,
    SecretRedactionRule,
    ToolAllowlistRule,
    apply_policy_config,
    load_policy_pack,
)
from penguiflow.steering import InMemoryGuardInbox

DEFAULT_POLICY = (
    Path(__file__).resolve().parents[1] / "docs" / "policies" / "default.yaml"
)


def test_load_policy_pack_env_override(tmp_path: Path) -> None:
    """Load policy pack env override."""
    content = textwrap.dedent(
        """
        policy_pack: "default"
        version: "1.0.0"
        gateway:
          mode: "enforce"
        environments:
          dev:
            gateway:
              mode: "shadow"
        """
    )
    path = tmp_path / "policy.yaml"
    path.write_text(content, encoding="utf-8")

    pack = load_policy_pack(path, env="dev")
    assert pack.policy_id == "default"
    assert pack.data["gateway"]["mode"] == "shadow"


def test_apply_policy_config_updates_rules_and_gateway(tmp_path: Path) -> None:
    """Apply policy config updates rules and gateway."""
    content = textwrap.dedent(
        """
        policy_pack: "default"
        version: "1.0.0"
        gateway:
          mode: "shadow"
          sync:
            timeout_ms: 10
            parallel: false
        tool_risks:
          "tool.a": "high"
        sync_rules:
          - id: "tool-allowlist"
            enabled: true
            config:
              denied_tools: ["tool.a"]
          - id: "secret-redaction"
            enabled: false
        """
    )
    path = tmp_path / "policy.yaml"
    path.write_text(content, encoding="utf-8")

    registry = RuleRegistry()
    registry.register(ToolAllowlistRule())
    registry.register(SecretRedactionRule())

    gateway = GuardrailGateway(
        registry=registry,
        guard_inbox=InMemoryGuardInbox(AsyncRuleEvaluator(registry)),
        config=GatewayConfig(),
    )
    builder = ContextSnapshotBuilder()
    router = DefaultRiskRouter()

    pack = load_policy_pack(path)
    apply_policy_config(registry, gateway, builder, router, pack)

    assert gateway.config.mode == "shadow"
    assert gateway.config.sync_timeout_ms == 10
    assert gateway.config.sync_parallel is False

    allowlist = next(
        rule
        for rule in registry.all_rules()
        if rule.rule_id == "tool-allowlist"
    )
    assert isinstance(allowlist, ToolAllowlistRule)
    assert allowlist.denied_tools == frozenset({"tool.a"})
    secret = next(
        rule
        for rule in registry.all_rules()
        if rule.rule_id == "secret-redaction"
    )
    assert secret.enabled is False


def test_default_policy_pack_waits_for_its_llm_before_rule() -> None:
    """The default pack applies as documented and waits for hf-jailbreak."""
    registry = RuleRegistry()
    for rule in (
        ToolAllowlistRule(),
        SecretRedactionRule(),
        InjectionPatternRule(),
        HuggingFaceJailbreakRule(),
    ):
        registry.register(rule)
    router = DefaultRiskRouter()
    gateway = GuardrailGateway(
        registry=registry,
        guard_inbox=InMemoryGuardInbox(AsyncRuleEvaluator(registry)),
        risk_router=router,
    )

    policy = load_policy_pack(DEFAULT_POLICY, env="prod")
    apply_policy_config(
        registry, gateway, gateway.context_builder, router, policy
    )

    routing = router.route(
        GuardrailEvent(event_type="llm_before", run_id="r"),
        ContextSnapshotV1(),
    )
    assert routing.async_wait_ms == 5000.0
