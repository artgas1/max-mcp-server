# Contributing / Как помочь проекту

Thanks for your interest! Issues and pull requests are welcome in English or Russian.

## Ground rules

- **No personal data anywhere**: no real phone numbers, tokens, `session.json`, names, chat titles or
  message texts in issues, PRs, tests, fixtures or logs. Use synthetic data (`+70000000000`, small ids,
  `example` hosts).
- Tests must not touch the network (the guard in `tests/conftest.py` enforces it). Do not log into a
  real account from tests or CI.
- New PyMax behaviour goes into `src/max_mcp_server/adapter/` only; the rest of the code works with
  the adapter DTOs. Unconfirmed protocol details are documented in `docs/protocol-notes.md`.
- Write tools must stay off by default and pass through the policy/limits in `tools/common.py`.

## Development

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pytest --cov
```

Commit messages: short imperative subject with an area prefix (`tools:`, `adapter:`, `docs:`, `ci:` …).
Add a line to `CHANGELOG.md` under *Unreleased* for user-visible changes.

## Release (maintainers)

1. Bump `__version__` in `src/max_mcp_server/__init__.py` (single source of truth) and `server.json`.
2. Move *Unreleased* entries in `CHANGELOG.md` to a new version section with the date.
3. Tag `vX.Y.Z` and push the tag: `release.yml` builds, publishes to PyPI via Trusted Publishing and
   creates a GitHub Release with the changelog section.
