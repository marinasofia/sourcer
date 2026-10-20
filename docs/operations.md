# Operations guide

How to run Sourcer locally, read what it tells you, debug it, and keep the
scheduled run healthy.

## Run locally

```bash
.venv/bin/pip install -e ".[dev]"
cp .env.example .env                              # ANTHROPIC_API_KEY
cp skills_vector.example.json skills_vector.json
cp profile.example.json profile.json              # optional: enables level fit
SOURCER_COST_CAP=0.05 .venv/bin/python -m sourcer.main
```

The digest on stdout lists ranked matches, the excluded list with reasons,
and the run totals. Diagnostics go to stderr through `logging`; set
`SOURCER_LOG_LEVEL=DEBUG` to see per-company fetch counts and retry reasons.

Cost-free run, useful for checking sources and rules:

```bash
SOURCER_COST_CAP=0 .venv/bin/python -m sourcer.main
```

With a cap of zero no Anthropic client is created, so the key is not needed.

## Read the run log

Every run appends one row to `run_logs`:

```bash
sqlite3 data/sourcer.db "SELECT run_ts, postings_seen, new_postings, excluded_count, \
  est_cost_usd, duration_s FROM run_logs ORDER BY id DESC LIMIT 10"
```

`node_metrics` holds per-node latency and counts as JSON; `exclusions` holds
the reasons. `parse_failure_detail` is only in the in-memory stats, so run
with `SOURCER_LOG_LEVEL=DEBUG` to see individual schema failures.

## Cost model

| Path | Model calls | Typical cost |
|---|---|---|
| Regex-filtered posting | 0 | 0 |
| Senior title | 0 | 0 |
| Duplicate hash | 0 | 0 |
| Twin (same company and title already scored) | 0 | 0 |
| Title triage | 1 per run, batched | under a cent |
| Parse and score | 1 per new posting | about 0.2 cents |
| Backlog through Batches API | 1 per posting | half price |

`SOURCER_COST_CAP` is checked before every call, so a run can overshoot by at
most one call. The scheduled run uses 0.01 USD; a local exploratory run uses
2.50 USD by default.

## Debug a scoring decision

1. Find the row: `python -m sourcer.track list | grep -i "<title>"`.
2. Look at the stored extraction:
   `sqlite3 data/sourcer.db "SELECT rationale, exclusion_reason, work_mode, start_date FROM postings WHERE id=<id>"`.
3. Re-run the rules in isolation:

```python
from sourcer import config
from sourcer.eligibility import eligible
parsed = {"title": "...", "employment_type": "full time", "start_date": "fall 2026", "work_mode": "hybrid", "location": "Austin, TX"}
eligible(parsed, config.load_profile())  # Verdict(ok, rule, reason, quote)
```

4. To re-score after changing the skills vector: `python -m sourcer.rescore`.

## Scheduled run

The private data repository runs at 13:00 UTC on weekdays. It checks out the
private code repository, installs the hash-locked dependencies, restores state,
and runs collection with a 0.01 USD estimated model budget. Monday discovery
probes up to 25 unresolved companies. The complete private export includes
company records, feedback, and notification state as well as postings and runs.

Telegram stays offline in scheduled runs. Browser collection is opt-in and is
not installed by the scheduler. Results are committed only to the data repository.
The workflow no longer publishes Pages or pushes aggregate data into the code repo.

## Common failures

| Symptom | Cause | Fix |
|---|---|---|
| `source fetch failures: [...]` in the digest | a board API is down or a slug changed | the run continues; fix the slug in `companies.json` |
| `parse/score fails` above zero | the model returned invalid JSON twice | inspect with DEBUG logging; usually a truncated posting |
| Everything skipped, `skipped_cost_cap` high | cap reached early | raise `SOURCER_COST_CAP` or run `sourcer.batch` for the backlog |
| `runs` tile on the dashboard shows 1 | database restored without history | fixed in 0.2.0; rerun `sourcer.restore` |
| Ashby rows never score | posting pages are client-rendered | fixed in 0.2.0; resolved through the posting API |

## Data hygiene

- `data/`, `skills_vector.json`, `profile.json`, `docs/data.json`, and
  `sample_output.txt` are gitignored here. The pre-commit hook and CI refuse
  them.
- The showcase export redacts employer names and fails closed on gap
  vocabulary. Run it locally before touching the sanitizer:
  `python -m sourcer.export_showcase /tmp/showcase.json`.
