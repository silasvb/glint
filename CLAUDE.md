# CLAUDE.md

Guidance for Claude Code when working in this repository.

glint is a Python CLI distributed as a **uv tool** (`uv tool install .`). The project is managed entirely with [uv](https://docs.astral.sh/uv/): dependencies, the virtualenv, running commands and building all go through it.

## Commands

```sh
uv sync                          # create/refresh .venv from uv.lock (includes the dev group)
uv run pytest                    # run the test suite
uv run pytest -k <name> -x       # run a subset, stop on first failure
uv run pre-commit install        # once per clone: run the hooks on every commit
uv run pre-commit run --all-files  # ruff lint (with fixes) + ruff format; gitleaks only sees staged changes
uv run pre-commit run gitleaks-history --hook-stage manual  # scan full git history for secrets
uv run glint show                # run the CLI from the working tree (no install needed)
uv run glint check --all-scenarios
uv build                         # build sdist + wheel into dist/
uv tool install --reinstall .    # install the current tree as the global `glint` tool
```

Run the CLI against `examples/` to exercise most features end to end:

```sh
cd examples && uv run --project .. glint check --all-scenarios
```

## Working with uv

- **Always go through `uv`.** Use `uv run <cmd>` instead of activating `.venv` or calling `python`/`pip`/`pytest` directly. `uv run` keeps the environment in sync with `uv.lock` before each command.
- **Never use `pip install`.** Add dependencies with `uv add <pkg>` (runtime) or `uv add --dev <pkg>` (dev group). Remove with `uv remove`. These update both `pyproject.toml` and `uv.lock`.
- **Commit `uv.lock`** alongside any `pyproject.toml` change. Don't hand-edit the lockfile; regenerate it with `uv lock` if needed.
- **Keep runtime dependencies minimal.** This is a tool installed into its own isolated environment; every runtime dependency adds install time and conflict surface. Prefer the standard library. Test/dev-only packages belong in `[dependency-groups] dev`.
- **Respect `requires-python`** (currently `>=3.10`). Don't use syntax or stdlib APIs newer than that (e.g. `tomllib`, `typing.Self`, `except*` are 3.11+). Check with `uv run --python 3.10 pytest` when touching anything version-sensitive.
- **Don't commit build or environment output**: `.venv/`, `dist/`, `__pycache__/`, `.pytest_cache/`.

## Packaging a uv tool

- The entry point is declared in `pyproject.toml` under `[project.scripts]` (`glint = "glint.cli:main"`). If you rename or move `main`, update it there.
- Uses the `src/` layout with hatchling (`[tool.hatch.build.targets.wheel] packages = ["src/glint"]`). Non-Python files the tool needs at runtime must live inside `src/glint/` and be read via `importlib.resources`, not paths relative to the repo — the installed tool has no access to the checkout.
- Before calling a packaging change done, verify the installed tool, not just `uv run`:
  ```sh
  uv build && uv tool install --reinstall . && glint --help
  ```
- Bump `version` in `pyproject.toml` for user-visible releases.

## CLI conventions

- `main()` returns `0` on success and exits non-zero on failure (`glint check` exits `1` on errors; fatal errors use `sys.exit("glint: ...")`). CI pipelines depend on these exit codes, so don't change them casually.
- Write results to stdout and diagnostics/errors to stderr, so output can be piped.
- Keep the CLI layer (`cli.py`) thin: argument parsing and output selection only. Put logic in importable modules so it can be tested without spawning a subprocess.
- Any new flag or command needs a matching update to `README.md`.

## Testing

- Tests live in `tests/` and run with `uv run pytest`. Add or update a test for every behaviour change or bug fix.
- Prefer testing module functions directly; for CLI behaviour, call `main([...])` with an argv list and use `capsys` rather than shelling out.
- Use `tmp_path` for any test that needs files on disk (CI YAML, `.glint.yml`, sourced scripts). Never write into the repo.
- Run the full suite before finishing a change.

## Code style

- Type hints on public functions; `from __future__ import annotations` where it helps on 3.10.
- Match the existing style of the surrounding module — naming, docstring density, error handling.
- Linting and formatting use ruff's default rules and formatter, run through pre-commit (`.pre-commit-config.yaml`). Run `uv run pre-commit run --all-files` before finishing a change. Don't add per-file ignores or `# noqa` to silence a finding without a reason.
- Don't add new linters, formatters or other tooling without asking.

## Safety

- Files exported from GitLab (`glab variable export > vars.json`) contain secrets. Never commit them, print their values, or include them in test fixtures — use fabricated names/flags only.
- The gitleaks pre-commit hook (`.gitleaks.toml`) scans staged changes for secrets, including a custom rule for GitLab variable exports. If it flags something, remove the secret; never bypass it with `--no-verify`. Allowlist a genuine false positive by adding its fingerprint to `.gitleaksignore`.

## Pull requests

Every time you create a PR (or push new commits to one), get it reviewed by the `code-reviewer` subagent (`.claude/agents/code-reviewer.md`):

1. After `gh pr create` succeeds, launch the `code-reviewer` agent with the PR number. Give it only the PR number. Don't pass it your reasoning or a summary of the change; the review should be independent of the conversation that wrote the code.
2. Relay its verdict and findings to the user exactly as it reports them, without softening or dropping any.
3. Don't fix the findings or post them on the PR unless the user asks.
