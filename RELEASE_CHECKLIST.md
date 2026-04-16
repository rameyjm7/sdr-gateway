# Release Checklist

## Pre-release quality

- [ ] `make lint`
- [ ] `make type`
- [ ] `make test-junit`
- [ ] Hardware regression tests run on available SDR set

## Security + config

- [ ] No secrets in git diff (`key.txt`, tokens, `.env`)
- [ ] `.env.example` up to date with new env vars
- [ ] Auth behavior validated (`/auth/verify`, websocket auth)

## Docs

- [ ] `README.md` updated for API/behavior changes
- [ ] `ARCHITECTURE.md` updated for major flow changes
- [ ] `TROUBLESHOOTING.md` updated for new failure modes
- [ ] `CONTRIBUTING.md` still matches CI/tooling

## Packaging + deploy

- [ ] `requirements.txt` / `requirements-dev.txt` updated as needed
- [ ] Systemd service script validated on clean host
- [ ] Rollback plan documented (previous tag/commit)

## Post-release

- [ ] Confirm service health
- [ ] Smoke test `/devices`, `/streams`, `/tx`, `/sweeps`
- [ ] Monitor logs + metrics for regressions
