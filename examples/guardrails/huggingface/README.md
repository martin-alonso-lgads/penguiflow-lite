# HuggingFace Prompt Injection Guardrail

This example demonstrates an async prompt-injection classifier using
HuggingFace transformers. Two model options are supported:
- `protectai/deberta-v3-base-prompt-injection-v2`
- `madhurjindal/Jailbreak-Detector-2-XL`

## Optional dependencies

```
uv pip install transformers torch
```

## Run

```
uv run python examples/guardrails/huggingface/flow.py
```

The flow runs a benign question and an injection attempt. The gateway's
`DefaultRiskRouter(llm_before_wait_ms=5000.0)` waits for the classifier on the
user input, so a STOP ends the run (`no_path`) before the LLM is called. With
the router's default wait of 0 ms the verdict arrives late and only applies at
the run's next guardrail check, which a one-step run never reaches.

If transformers is not installed, the guardrail rule fails open and both runs
finish with `answer_complete`.
