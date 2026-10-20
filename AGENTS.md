# Sourcer: guide for coding agents

Sourcer is a job matching agent: a LangGraph pipeline that pulls postings from
company career boards, filters what cannot fit, and scores the rest against a
weighted skills vector with the Claude API. This file and AGENTS.md are
identical on purpose. Edit both together.

## Layout

```
sourcer/                  the package. config, db, llm, and log stay at the top level
  config.py               settings read once at import, policy regexes, profile loader
  eligibility.py          eligible(): every rule about the candidate, one Verdict
  db.py                   storage facade; SOURCER_BACKEND selects sqlitedb or snowdb
  sqlitedb.py, snowdb.py  storage backends: postings, run_logs, companies, applications
  llm.py                  Claude client, usage accounting, schema-validated calls
  log.py                  logging setup
  sources/                one adapter per ATS; each exposes fetch_postings(company)
    greenhouse.py, lever.py, ashby.py                  live, registered in FETCHERS
    workable.py, smartrecruiters.py, rippling.py        registered adapters
    careers_page.py                                  opt-in static browser fallback
    simplify.py           SimplifyJobs community lists, metadata only, never scored
    _common.py            HTTP, HTML cleanup, and date helpers shared by adapters
  pipeline/               source -> dedupe -> parse -> score -> output
    state.py              the State TypedDict and per-node bookkeeping
    source.py             source node: fetch boards, prefilter, title triage, listings
    dedupe.py             dedupe node
    filters.py            timing flag; re-exports the eligibility rules for the pipeline
    record.py             judge() and save_score(): one model read to stored fields, every path
    scoring.py            parse and score nodes, plus the scoring service for HTTP and MCP
    output.py             output node
    graph.py              LangGraph wiring
  apply/                  verify.py (listing verification); tailor.py, prefill.py reserved
  bot/                    telegram.py, allowlisted digest and feedback
  discovery/              company discovery adapters and quality gates
  probe/                  ATS and careers-page discovery CLI
  main.py, serve.py, mcp_server.py, lambda_handler.py            entry points
  track.py, alerts.py, rescore.py, restore.py, export.py,
  export_public.py, export_showcase.py, score_listings.py,
  batch.py, evaluate.py, digest.py                               operator tools
docs/                     operator docs, ADRs, operator guides and retired dashboard assets
tests/                    pytest; no network, no API key; coverage gate 90% on the package
```

## Rules

- Import layering is a test (`tests/test_architecture.py`). A module imports
  only its own layer or below: config and log; then llm, sources, and
  storage; then pipeline; then entry points and tools. Every module must be
  assigned a layer there.
- Storage backends are reached only through `sourcer.db`.
- Move code, do not rewrite it, when changing layout. The private repository
  imports `sourcer.config`, `sourcer.db`, `sourcer.llm`, `sourcer.eligibility`
  and `sourcer.pipeline` modules directly; keep those importable.
- Every path that scores a posting builds its stored fields with
  `pipeline.record.judge` and writes a stored row with `save_score`. Do not
  apply the rules or the timing note by hand anywhere else.
- New ATS adapters go in `sourcer/sources/`, expose `fetch_postings(company)`,
  and are registered in `sourcer.sources.FETCHERS` only when they return real
  data. Reserved stubs raise `NotImplementedError`.
- The companies table (id, name, domain, ats_type, ats_slug, source,
  added_at, quality_signals as JSON, submit_enabled default false) is seeded
  from `companies.json` by `main.build_state` on every run through
  `db.upsert_companies`. Submission authorization is private operator state,
  is rejected in company input, and is revoked when a company is removed.
  Nothing submits an application.
- Zero model calls when `SOURCER_COST_CAP=0`; tests use a fake client.
- No em dashes or en dashes anywhere. A hygiene check fails the build.
- Never commit `skills_vector.json`, `profile.json`, `docs/data.json`,
  resumes, kits, or databases. `scripts/check_public_hygiene.py` enforces it.

## Two repositories

This code repository is public; the data repository is private. The data
repository installs this package at a pinned commit. Nothing personal belongs
here: no operator facts in code, comments, tests, or commit messages. Scheduled exports stay only in the data
repository. Telegram defaults to dry run and no application submission exists.

## Commands

```bash
.venv/bin/pytest --cov
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy
SOURCER_COST_CAP=0 .venv/bin/python -m sourcer.main    # full pipeline, zero model calls
```
