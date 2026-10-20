# 0003. A hard per-run cost cap checked before every model call

Date: 2026-08-28
Status: accepted

## Context

One run admitted 53 mostly senior titles through triage, parsed each of
them, and then excluded them on the title, costing 0.56 USD for nothing.
The operator's requirement for the scheduled run is one cent or less.

## Decision

`SOURCER_COST_CAP` is a hard ceiling in USD. A running estimate from token
usage (input, output, cache write, cache read at their published rates) is
compared against it before every model call: title triage, parse and score,
backlog scoring, rescoring. Once reached, remaining postings are counted as
`skipped_cost_cap` and stored unscored, never dropped. A cap of zero runs
the whole pipeline with no client at all.

The scheduled run sets the cap at the job level so every module inherits
it. Local exploratory runs default to 2.50 USD.

## Consequences

- A run can overshoot by at most one call.
- Unscored postings accumulate; `sourcer.batch` scores the backlog at half
  price with a dry-run estimate first.
- Any new model call must check the cap; the pull request template asks
  for it.
