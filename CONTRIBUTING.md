# Contributing

Thanks for helping. Small, focused changes are easiest to review.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
make test
```

Tests run offline. Please keep it that way: use fakes for the LLM, the network and the
Instagram API (see `tests/test_auto_gate.py` and `tests/test_telegram_notify.py` for the style).

## Rules of this codebase

- **The LLM writes text only.** Decisions (layout, approval, publishing, severity) belong in code.
- **Fail safe.** Uncertain means hold, not approve. Never add a mock fallback for a failed LLM provider.
- **Do not weaken a gate to make a candidate pass.** Add a repair step in front of it instead.
- Config over constants: thresholds and limits live in `config/*.yaml` and `channel/config/*.yaml`.
- Never put secrets, tokens or personal data in code, tests, fixtures or logs. Tests must never reach
  a real service (see `tests/conftest.py`).
- Dependency direction is `channel → src` only; a test enforces it.

## Pull requests

1. Add or update tests for the behavior you change.
2. Run `make test`.
3. Describe what changed and why, and note anything you could not verify (for example rendering needs Chrome).

## Ideas that are welcome

New categories and sources, a Linux scheduler recipe, other LLM providers, more layouts, translations.
