# CLAUDE.md

Scientific modelling project (data science / energy / finance / economic).
Full team handbook: `docs/HANDBOOK.md`. Algorithm docs: `docs/ALGORITHM.md`.

## Commands

```bash
uv sync --all-extras           # install
uv run pytest                  # tests
uv run pytest --cov=src        # tests + coverage
uv run ruff check . --fix      # lint + autofix
uv run ruff format .           # format
uv run mypy src/               # type-check
```

If `uv` is not yet adopted, fall back to `pip install -e ".[dev]"` and `pytest` / `ruff` / `mypy` directly. Don't introduce `setup.py`, `requirements.txt`, `flake8`, or `black` configs — `pyproject.toml` is the single source of truth.

Where the project has a browser client or an MCP server, `npm run dev` / `npm run build` / `npx tsc --noEmit` sit alongside the above, and the backend↔frontend type contract is part of the definition of done. Verify a UI change in the running app, not only in tests — and against a server actually serving current code.

## Conventions

- **Python 3.11+**, type hints mandatory on public functions, Google-style docstrings.
- **Math docstrings**: include an `Algorithm:` section with LaTeX (`$$...$$`) primary and an ASCII fallback line. Define every symbol with units.
- **Variable names**: descriptive in general (`temperature_kelvin`), but **single letters are OK** when they mirror equations (`T`, `x`, `ε`, `dt`, `i`, `j`). Don't fight the math.
- **No hardcoded values**: load via `src/<pkg>/config.py` from `.env`. Mirror every var into `.env.example`.
- **Reproducibility**: pin random seeds (`numpy.random.default_rng(seed)` over the legacy global API). Commit `uv.lock`. Pin upstream versions.
- **Units**: use `pint` for any quantity with physical units (energy, power, currency rates, time-of-day). Don't pass bare floats across module boundaries when units matter.
- **Numerical correctness**: when changing math, add a test against an analytical solution OR a captured baseline (`np.testing.assert_allclose` with explicit `rtol`/`atol`).

## Logging

Use `src/<pkg>/logger.py`. Log shape and dtype, never full arrays. Never log secrets, PII, or raw data rows.

| level | use for |
|-------|---------|
| DEBUG | branch decisions, scalar values, shapes |
| INFO  | milestones (data loaded, fit complete) |
| WARNING | recoverable degradation |
| ERROR | failure that returns or skips |
| CRITICAL | abort |

## Tests

Pytest. New features need tests. Aim for **meaningful** coverage of math correctness, not a line-coverage %. For numerical code, regression tests against analytical solutions beat 100% line coverage every time.

## Git workflow

- Feature branch → PR → CI green → merge to main → delete branch.
- **Conventional commits**: `feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`.
- **Self-review before merge**: re-read the diff. For math changes, paste before/after equations into the PR description. CI passing ≠ math correct.
- Never `--force` push to main. Use `git revert` to undo merged commits.

## Project layout

```
src/<pkg>/
  core/       algorithms (no I/O)
  data/       loaders, validators, transforms
  config.py   loads .env, validates types
  logger.py   centralized logging
tests/        mirrors src/
docs/         ALGORITHM.md (math), HANDBOOK.md (team standards), API.md
.env, .env.example
pyproject.toml    single source of truth
```

See `docs/HANDBOOK.md` for: full directory layout, docstring template, ready-to-copy `config.py` / `logger.py` / CI workflow, code review checklist, deprecation strategy, experiment tracking patterns.

## Documentation hygiene

Markdown sprawl is the failure mode: a pile of documents nobody reads because none is authoritative.

- **One index per documentation set**, and **every document is reachable from it** by following links. A document that isn't reachable has no purpose — delete it.
- **One concern per document, one home per fact.** Specifications, current status, and history are three different documents. A fact copied into a second document will be wrong within a week.
- **No `_v2`, `_final`, `_old`, or dated duplicates.** A superseded document is deleted or archived, never left beside its replacement. No `ALL_CAPS_` filename prefixes.
- Before creating a document, search for one that already covers it and extend that instead.

## Contracted research engagements

Where the work is bound by a contract, proposal, or funding agreement, the **contract and proposal are the truth source** and the governance set lives under `claude-docs/` — never scattered across the repo root:

```
claude-docs/
  README.md   charter.md   tracker.md        (exactly three files at the root)
  phases/  stages/  process/  toolbox/  team/  reports/  engagement/  dashboard/  log/
```

Five agents own it: **`research-director`** (charter → phases → stages → process → toolbox → tracker → team, and re-reads the contract at every gate), **`consultant`** (the only customer-facing role; drafts only, never sends, never accepts scope), **`report-manager`** (process governance + the HTML progress and team dashboards), and the two interim reporters — **`log-reporter`** (what was done and what failed) and **`result-reporter`** (the analysis as a journal article, with a conference-presentation deck). Reporting is step by step, at every process step, stage exit and phase gate — never assembled at the end from memory. Non-negotiable throughout: every figure traces to a data-register row or a numbered assumption, nothing is hardcoded, findings are associations rather than causes, and results carry a range rather than a point estimate.
