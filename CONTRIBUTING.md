# Contributing to google-health-mcp

Thanks for your interest in contributing. This is a community MCP server for the Google Health API.

## Getting started

### Prerequisites

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- No Google account or Cloud project is needed to develop or to run the tests - they are fully offline. You only need one to run the server against real data, which the README's setup section covers.

### Set up the dev environment

```bash
git clone https://github.com/partymola/google-health-mcp
cd google-health-mcp
uv venv --python 3.13 .venv
uv pip install -e ".[dev]"
```

Command lines here assume a POSIX shell. On Windows the interpreter is `.venv\Scripts\python`, and the hook install below needs a shell that provides `ln`, such as Git Bash.

### Install the pre-commit hook

The repo ships with `scripts/check-no-data.sh`, which blocks commits that contain databases, tokens, or other secrets:

```bash
ln -sf ../../scripts/check-no-data.sh .git/hooks/pre-commit
```

Please install it before your first commit.

### Run the test suite

```bash
.venv/bin/python -m pytest tests/ -v
```

CI runs this on Linux, macOS and Windows.

Tests are fully offline - no real API calls, no real tokens. Autouse fixtures in `tests/conftest.py` enforce that rather than trusting each test to: your own credential files are replaced with an empty directory, and a test that reaches the network fails. Fixtures use fictional data and fixed past dates; never paste real health measurements into tests.

### Run lint and formatting checks

```bash
.venv/bin/python -m ruff check src tests
.venv/bin/python -m ruff format --check src tests
```

## Making changes

- **Open an issue first** for non-trivial changes (new tools, schema migrations, new data types, breaking changes). Small fixes (typos, bug fixes, docs) can go straight to a PR.
- Keep PRs small and focused.
- Add or update tests for any behaviour change.
- **Changing the database schema takes two edits, not one**: the column in `SCHEMA`, and an `ALTER` in `db.MIGRATIONS` so databases created by an earlier release get it too. The tests apply your migration to every schema in `tests/schema_baselines/` and fail if a column does not arrive; AGENTS.md explains what that prevents. A whole new table needs no migration - `CREATE TABLE IF NOT EXISTS` handles it.
- Run `pytest tests/ -v` before opening a PR.

## Releases (maintainers)

1. Bump `version` in `pyproject.toml`, run `uv lock` so the tracked lockfile records the new version, update the `"version"` in the README's `doctor --json` example payload, and turn the `## Unreleased - X.Y.Z` CHANGELOG heading into `## X.Y.Z - YYYY-MM-DD`. Headings carry no link references: an undefined one renders as literal brackets, which is what a deleted release history leaves behind. The lockfile and the README example are both checked by tests, so a missed one fails rather than shipping stale - the README's matters because the surrounding text tells a consumer to gate on that field.
2. If `db.SCHEMA` differs in tables or columns from the newest file in `tests/schema_baselines/`, copy it in as `X.Y.Z.sql`. That file is what proves the next release's migrations reach a database created by this one. Never edit one already there.
3. Push to `main` and wait for CI to pass on that commit.
4. Tag it `vX.Y.Z` and push the tag by name.
5. Create the GitHub Release.

Step 5 is what publishes: `publish.yml` runs on `release: published`, not on the tag push, so the tag on its own ships nothing. It builds the distribution, checks the sdist for secret-shaped files, uploads to PyPI via Trusted Publishing, then registers the release in the MCP registry.

**Do not hand-edit `server.json`'s `version` or `packages[0].version`.** The workflow rewrites both from the tag before publishing, so the values committed to the repo are deliberately left behind and are not a bug. To see what actually published, query the registry rather than reading the file:

```bash
curl -s "https://registry.modelcontextprotocol.io/v0/servers?search=io.github.partymola/google-health-mcp"
```

The registry step can fail on its first attempts while PyPI's description catches up; it retries, and a failure there means the PyPI upload still succeeded. `--version` reads the installed package metadata, so it follows `pyproject.toml`.

## Pull requests

- Branch off `main`.
- Reference any related issue.
- Maintainer aims to reply within ~7 days. Feel free to bump if you don't hear back.

## Reporting issues

Helpful details to include:

- Python version (`python --version`)
- MCP client (Claude Desktop, Claude Code, other)
- The device the data comes from, if relevant
- Steps to reproduce
- Relevant log output, with any tokens, user IDs, or measurement values redacted

## Security

Please do not open a public issue for credential, OAuth-flow, or token-leak issues. Use [GitHub's private vulnerability reporting](https://github.com/partymola/google-health-mcp/security/advisories/new) instead.

## License

By contributing, you agree that your contributions are licensed under GPL-3.0-or-later, the project's license.
