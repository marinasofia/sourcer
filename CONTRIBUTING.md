# Contributing to Sourcer

Thanks for taking the time. Sourcer is a small, opinionated codebase and it stays
that way on purpose. This page explains how to get a working setup, what the
checks expect, and how changes get merged.

## Ground rules

- Be kind. The [Code of Conduct](CODE_OF_CONDUCT.md) applies everywhere in this
  project.
- Never commit secrets, resumes, skills vectors, or scored data. The public
  repo holds code and aggregate statistics only. See [SECURITY.md](SECURITY.md).
- Every change that touches behaviour ships with a test and, when it changes
  something a user sees, a line in [CHANGELOG.md](CHANGELOG.md) under
  `Unreleased`.
- Keep pull requests small. One concern per PR is the norm.

## Development setup

Python 3.12 or newer is required.

```bash
git clone https://github.com/marinasofia/sourcer.git
cd sourcer
python3 -m venv .venv
.venv/bin/pip install -e ".[dev,aws,snowflake,onboard]"
cp skills_vector.example.json skills_vector.json  # edit the weights
.venv/bin/pre-commit install
```

`uv` works too: `uv venv && uv pip install -e ".[dev,aws,snowflake,onboard]"`.
No API key is needed for offline checks. Create a local `.env` only when
intentionally working with a live model account.

## Running the checks

The same commands run in CI. All of them must pass before a PR is reviewed.

```bash
.venv/bin/ruff check .                 # lint
.venv/bin/ruff format --check .        # formatting
.venv/bin/mypy                         # types (config in pyproject.toml)
.venv/bin/pytest                       # unit tests, no network, no API key needed
.venv/bin/pytest --cov --cov-report=term-missing   # coverage, fails under 90%
```

Run the deterministic evaluation with its bundled profile:

```bash
.venv/bin/python -m sourcer.evaluate
```

Use `--profile path/to/profile.json` to evaluate a deliberate policy override.
The default fixture is independent of the operator's local profile.
To match CI dependencies, install `uv`, create `.venv`, and run
`uv pip sync --python .venv/bin/python requirements-dev.txt`. Optional AWS and
Snowflake dependencies are needed only when exercising those integrations.

When you change an exclusion or level-fit rule, add a labelled case to
`evals/labelled_postings.json` in the same pull request.

`pre-commit run --all-files` runs lint, format, and a secrets scan in one go.

## Tests

- Tests never hit the network or the Claude API. Use the fake client in
  `tests/conftest.py` and patch `requests.get` for source fetchers.
- Tests never depend on files outside the repo. If a rule reads a profile or
  a skills vector, inject it.
- Property-based tests (Hypothesis) live next to the unit tests for the
  deterministic policy functions in `sourcer/config.py` and `sourcer/pipeline/filters.py`.
- Anything that needs real credentials is marked `@pytest.mark.integration`
  and skipped by default.

## Style

- `ruff` owns formatting and import order. Do not hand-format.
- Type hints on every public function. `mypy` runs in CI.
- No em dashes or en dashes anywhere in code, comments, docs, or commit
  messages. Use commas, colons, or periods.
- Prompts sent to the model live in `sourcer/llm.py` as module constants so a
  diff shows exactly what changed.
- Print only from CLI entry points. Library code uses `logging`.

## Commits and pull requests

- Write commit messages in plain English, present tense, explaining why. No
  attribution trailers.
- Branch from `main`, open a PR against `main`, fill in the template.
- CI must be green before merge. The recommended branch rules and actual
  check inventory are in [docs/maintenance.md](docs/maintenance.md). Protection
  was not enabled at the September 4, 2026 audit. Require independent review
  when another maintainer is available.

## Releases

Versions follow [Semantic Versioning](https://semver.org). To cut a release:

1. Move the `Unreleased` entries in `CHANGELOG.md` under a new version
   heading with today's date.
2. Bump `version` in `pyproject.toml`.
3. Merge, then tag: `git tag -a vX.Y.Z -m "vX.Y.Z" && git push --tags`.

The release workflow builds the wheel and publishes a GitHub Release with the
changelog section as its notes.

## Where things live

| Path | What |
|---|---|
| `sourcer/` | the package: pipeline nodes, sources, LLM helpers, storage backends, CLIs |
| `tests/` | pytest suite |
| `docs/` | GitHub Pages dashboard plus engineering docs (`docs/adr/`, guides) |
| `k8s/`, `deploy/`, `Dockerfile*` | deployment surfaces |
| `.github/` | CI workflows, templates, Dependabot |

Questions? Open a [discussion issue](https://github.com/marinasofia/sourcer/issues/new/choose).
