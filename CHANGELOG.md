# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `sourcer.eligibility.eligible(posting, profile)` is the one function for
  every rule about the candidate. It returns a `Verdict` with a stable rule
  code, the reason in plain words, and the posting's own words when a rule
  read them; `RULES` is the one map from code to label. Scoring, listing
  scoring, evaluation and rescreen all call it; `check_exclusion`,
  `rescreen.reason_for` and `config.eligibility_conflict` remain as thin
  wrappers.
- A contract role is eligible when it is full time and starts in the target
  summer or names target year graduates.

- Dedicated Workday, Eightfold, and Oracle Recruiting adapters are registered
  for configured company boards.
- Weekly company discovery from Built In NYC, YC, HN Who is Hiring,
  SimplifyJobs, and configured Getro VC portfolios.
- Workable, SmartRecruiters, Rippling, and optional static careers-page collection.
- Early-career admission gates for ATS boards; the strict 21-day freshness
  gate remains for Simplify community listings.
- Allowlisted Telegram digests, company URL intake, thumbs feedback, shortlist
  actions, and aggregate weekly source reports. Telegram defaults to dry run.
- Bounded preference reranking that cannot override hard filters.
- Locked daily and weekly orchestration commands.
- A `companies` table (id, name, domain, ats_type, ats_slug, source, added_at, quality_signals, submit_enabled) in both storage backends, seeded from `companies.json` on every run. Submission authorization remains private operator state and removed companies are disabled.
- Reserved `apply.tailor` and `apply.prefill` seats.
- `sourcer probe <company name or URL>` with rate-limited ATS discovery, persistent results, unresolved URL audit trails, and `--all` for companies without an ATS type.
- `CLAUDE.md` and `AGENTS.md` describing the layout for coding agents.

### Changed

- One freshness rule, `config.is_fresh`: a role its company's own board still
  lists is never too old (age only lowers its rank); otherwise an undated
  posting is fresh and a dated one must be inside `FRESHNESS_DAYS`, now 30
  days by default (was 21). Behavior changes: the Simplify lists keep roles up
  to 30 days old and keep undated ones; the Telegram digest keeps undated
  rows and uses 30 days; the listing scorer uses 30 days; Eightfold and Oracle
  no longer skip undated roles, like Workday. `LISTING_SCAN_DAYS` is only the
  paging bound for paged boards. `apply.recheck.why_not_send` loses its unused
  `max_age` argument. `posting_is_fresh` stays as a wrapper for dated
  postings.
- One scoring path: `pipeline.record.judge` turns a model read into the stored
  fields (intern title, level grade, the rules read with the posting's own
  text, the timing note) and `save_score` writes a stored row with its trace
  event. The pipeline, the listing scorer, the batch scorer and rescore all
  use them. The listing and batch scorers now read the posting's text for the
  rules, which they skipped before.
- The program term check (`program_ended`, `TERM_RE`) has one home in
  `config`; `apply.recheck` and the Simplify term gate use it. The intern
  title pattern is `config.INTERN_TITLE_RE`, shared by scoring.
- Removed `config.target_window_in_scope`, which only tests called;
  `target_window_evidence` is the live check.

- Document current collection behavior: roles on live company boards are kept
  whatever their age or date; titles go through a title check that reads the
  profile's own words instead of a role name list. The age-penalty
  helper is used by the private non-ML iMessage ranking, not stored model scores.
- Document immutable scheduler promotion and the private collection-success
  indicator; these docs do not claim a successful deployment or passing CI.
- Replace unavailable private-repository CodeQL checks with free, hash-locked
  Bandit and offline zizmor scans, including unsafe-input scanner tests.
  Pin GitHub Actions to commit hashes and disable checkout credential persistence.
- Make the code repository private and remove GitHub Pages deployment and
  committed dashboard data. Scheduled results remain only in the private data repository.
- Dedupe on normalized company, title, and location. Direct ATS data replaces
  an aggregator row while preserving tracking state.
- Restructure the package by concern: `sources/` (one adapter per ATS, each exposing `fetch_postings`), `pipeline/` (state, source, dedupe, filters, scoring, output, graph), and `apply/` (verify). Code was moved, not rewritten. `sourcer.graph`, `sourcer.nodes`, `sourcer.scoring`, and `sourcer.verify` remain as compatibility shims (ADR 0009).

### Renamed

- Rename the application, Python package, command, configuration prefix, dashboard, and deployment examples to Sourcer. The distribution is now `sourcer-jobs`.
- Document migration of existing data, callers, and deployment settings.


### Fixed

- SQLite connections wait up to 30 seconds for another writer and use write
  ahead logging, so the server, the morning run and the extension no longer
  fail with "database is locked" when they write at once.
- Apply mandatory description eligibility checks to the shared exclusion path
  used by pipeline and HTTP/MCP scoring, including explicit location conflicts. Keep the original text in ad hoc
  scoring so mandatory requirements cannot disappear in the model summary.

- Reject common explicit non-NYC office or residency requirements in descriptions
  before AI triage and scoring, even when the board labels the role remote.

- Restrict new collection to internships and entry-level candidates. Reject
  common explicit experience requirements above two years before AI triage,
  and hard-exclude parsed mid/senior or over-limit roles before recommendation.
- Admit regular full-time roles without requiring a new-grad label, and honor
  explicit `earliest_start_full_time` availability from the runtime profile.
- Preserve restored tracking metadata; share HTTP/MCP scoring policy, validate model outputs, and enforce unrounded model admission checks.
- Load dotenv before settings and evaluate against the bundled fixture profile by default.

### Changed
- Added an offline quickstart and a reproducible fixture-profile evaluation command.
- Corrected security, configuration, and branch-protection guidance.
- Updated community templates and documented the implementation follow-up.

### Removed

- The `graph`, `scoring` and `verify` compatibility shims are removed; import
  `sourcer.pipeline.graph`, `sourcer.pipeline.scoring` and `sourcer.apply.verify`.
- The last compatibility shim, `sourcer.nodes`, is removed; import from
  `sourcer.eligibility` and the `sourcer.pipeline` modules.

## [0.2.0] - 2026-09-03

### Added
- Location policy: NYC or genuinely remote US, decided by
  `config.location_in_scope` across every ingest path.
- Availability rule: full time roles that start before graduation are
  excluded when a candidate profile is present (`config.starts_before_graduation`).
- Batched title triage: one Haiku call per run classifies titles the regex
  missed, gated by the cost cap and failing open.
- Twin reuse: an already scored posting with the same company and title
  transfers its score without an API call.
- `sourcer.batch`: backlog scoring through the Message Batches API at half price,
  with `--dry-run` cost preview.
- `work_mode` field (remote, hybrid, onsite, unspecified) extracted per posting;
  hybrid or onsite outside NYC is excluded after parsing.
- Prompt-cache marker gated on the model's minimum cacheable prefix.
- Packaging via `pyproject.toml` with pinned `requirements.txt` and
  `requirements-dev.txt` lockfiles; `sourcer` console script.
- Structured logging through the standard `logging` module across the package.
- Ruff, mypy, pre-commit, Dependabot, CodeQL, pip-audit, coverage gate, Docker
  build verification, and release automation in CI.
- Community health files: CONTRIBUTING, CODE_OF_CONDUCT, SECURITY, issue and
  pull request templates, CODEOWNERS.
- Architecture decision records under `docs/adr/`, deployment and operations
  guides under `docs/`, and a model card for the scoring use of the model.
- `sourcer.evaluate`: evaluation harness replaying a labelled posting set
  (`evals/labelled_postings.json`) through the exclusion and level-fit rules,
  with an optional `--model` pass reporting score band agreement. The
  deterministic pass runs in the test suite.
- `sourcer` console script with `--version` and `--cost-cap`.
- Test suite at 100% line and branch coverage, including property-based
  tests, an end-to-end pipeline run with a fake client, and an import
  layering test.

### Changed
- Senior titles are short-circuited before parsing so they never cost a
  model call.
- Ashby's `isRemote` flag appends a bare `Remote` token; the scope check
  decides from the full location list instead of assuming US.
- Candidate-specific wording removed from prompts and comments; the
  candidate description comes from the profile.
- The candidate profile is injected into the exclusion rules instead of read
  from disk inside them.
- The `snowflake` extra requires connector 4.x with `cryptography` 50 and
  `pyopenssl` 26 or newer; the 3.x connector capped `pyopenssl` below the
  releases that fix PYSEC-2026-2268 and PYSEC-2026-2269.

### Fixed
- Ashby posting pages are resolved through the posting API instead of scraped;
  the client-rendered HTML never contained the description, so every Ashby
  backlog row failed and was retried forever.
- Tests no longer depend on a profile file existing outside the repository.
- Public dashboard counted only the current run in the `runs` tile.
- Postings already parsed and paid for were dropped by the score node once
  the cost cap was reached; they are now stored.
- A SimplifyJobs list outage lowered the companies-covered count, and one
  dead list blocked the other; lists are fetched independently.
- ISO start dates with an impossible month (`2027-13`) no longer resolve to
  a start term.
- The package logger bound `sys.stderr` at first use; it now resolves it at
  emit time.

## [0.1.0] - 2026-08-24

### Added
- LangGraph pipeline: source, dedupe, parse, score, output.
- Greenhouse, Lever, and Ashby board sources; SimplifyJobs internship and
  new-grad lists as metadata-only listings.
- Claude Haiku parse and score in one schema-validated call with a single
  corrective retry.
- Deterministic exclusion rules: senior titles, five or more years required,
  immediate-start full time, contract roles.
- Profile-driven level fit (fit, stretch, misfit) with auto-archive of misfits.
- SQLite storage with additive migrations; Snowflake backend behind
  `SOURCER_BACKEND=snowflake`.
- Hard per-run cost cap, token and cache accounting, per-node latency metrics.
- GitHub issue alerts for high scores and internship or new-grad postings.
- Source verification (`sourcer.verify`) with dead-link auto-archive.
- Public dashboard on GitHub Pages fed by an aggregate-only export and a
  strengths-only showcase export with a fail-closed leak scan.
- MCP server (`score_posting`, `lookup_scored`), stdlib HTTP scoring service
  with Kubernetes manifests, AWS Lambda container image with S3 state.

[Unreleased]: https://github.com/marinasofia/sourcer/commits/main
[0.2.0]: #020---2026-09-03
[0.1.0]: #010---2026-08-24
