# Contributing

## Development setup

```bash
cd /home/jake/workspace/SDR/sdr-gateway
python3 -m venv .venv
source .venv/bin/activate
make install-dev
```

## Quality gates

Run before opening a PR:

```bash
make ci
```

This runs:
- lint (`ruff`)
- type checks (`mypy`)
- unit tests with JUnit + coverage report (`pytest`, `pytest-cov`)

## Test policy

- Unit tests go in `tests/` and should not require SDR hardware.
- Hardware tests must be explicitly marked `@pytest.mark.hardware`.
- Keep hardware tests opt-in via env vars.

## Commit hygiene

- Keep commits focused and atomic.
- Do not commit secrets, IQ captures, or local data dumps.
- Use `.env.example` for documented config defaults.

## Pre-commit (recommended)

```bash
pip install pre-commit
pre-commit install
pre-commit run --all-files
```
