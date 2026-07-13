# Honeydew Validate Workspace Action — Development Guidelines

This repository contains a GitHub Action that validates
[Honeydew](https://honeydew.ai) semantic-layer workspaces via the
[Honeydew GraphQL API](https://honeydew.ai/docs/integration/graphql-api).

## Repository Structure

- **`action.yml`** — the composite action definition (inputs, env wiring, branding)
- **`validate.py`** — the action's entire logic; runs with `python3` on the runner
- **`test_validate.py`** — pytest tests for `validate.py`
- **`.github/workflows/ci.yml`** — CI (tests, formatting, typing)

## Hard Constraints

- **`validate.py` must use only the Python standard library.** The action runs
  directly on customer runners without a `pip install` step — never add runtime
  dependencies.
- **No checkout requirement.** The action must keep working without
  `actions/checkout`; everything it needs comes from environment variables and
  the Honeydew API.
- **Never put untrusted GitHub event data into shell commands.** Inputs flow
  into Python via `env:` only.

## Python Guidelines

- Target Python 3.12+ syntax on runners; type-check with mypy (strict) at 3.12.
- Use modern type syntax: `X | None`, `list[str]`, `dict[str, int]` — import
  from `typing` only for advanced types (`typing.Any`, `typing.NoReturn`,
  `typing.cast`).
- Formatting and linting are enforced by pre-commit (black, ruff with ALL rules
  including import sorting — see `ruff.toml` — yamllint, mypy, and more).
  Lines stay under 120 characters (the ruff limit; black wraps sooner).
- Use keyword-only arguments (`*` separator) for functions with multiple parameters.
- Module-level constants use ALL_CAPS.
- Use the walrus operator for assign-then-check patterns.
- Write self-explanatory code; comment only what the code cannot say.

## Testing

- Tests use pytest. Run with `pytest -v`.
- Parametrize similar tests with `@pytest.mark.parametrize`, attaching ids via
  `pytest.param(..., id="...")` — never copy-paste test bodies.
- Assert the full output with `==` (no partial `in`/`len` checks).

## Checks

Run before committing (CI runs the same):

```bash
pre-commit run --all-files
pytest -v
```

Install the git hook once with `pre-commit install`.

## Releases

Customers pin the action as `honeydew-ai/validate-workspace-action@v1`.
After merging changes, create/move the `v1` major tag to the release commit.
