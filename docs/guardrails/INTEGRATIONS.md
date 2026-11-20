# Guardrails Integrations

This document describes opt-in guardrail integrations and local examples. These
examples do not add dependencies to the core library. Install optional packages
only when you run the example scripts.

## Prompt Injection (HuggingFace)

Recommended models:
- protectai/deberta-v3-base-prompt-injection-v2
- madhurjindal/Jailbreak-Detector-2-XL

Use the example in `examples/guardrails/huggingface` to wire the async rule.
The rule fails open when transformers is not installed. The example builds the
gateway with `DefaultRiskRouter(llm_before_wait_ms=5000.0)`, so the classifier's
STOP ends the run (`no_path`) before the LLM call; with the default wait of 0 the
STOP arrives late and only applies at the run's next guardrail check.

## Scope / Off-Topic Detection (Analytics Assistant)

This example provides a binary in-scope vs out-of-scope classifier for an
analytics-focused agent. It is designed to be trained locally and loaded into
memory at agent startup.

Highlights:
- Few-shot seed prompts expanded with LLM augmentation to 100-200 samples.
- Lightweight model (embeddings + LogisticRegression) for fast CPU inference.
- Async guardrail uses RETRY guidance rather than hard STOP. A late async RETRY is dropped,
  so it only applies when the risk router waits for the rule on `llm_before`. The example
  sets `DefaultRiskRouter(llm_before_wait_ms=500.0)`; the default wait is 0.
- Classification input is augmented with context to handle short utterances
  like "yes, go on" or "continue".

See `examples/guardrails/scope_classifier` for a runnable flow and training
script. The runtime rule falls back to no-op if optional ML dependencies are
missing.
