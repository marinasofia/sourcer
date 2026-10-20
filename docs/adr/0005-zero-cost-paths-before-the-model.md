# 0005. Dedupe, senior short circuit, and twin reuse run before any model call

Date: 2026-08-26
Status: accepted

## Context

Boards re-list the same role under several urls (per location, refreshed
postings) and most postings on a board are out of scope. Paying the model
to learn that a posting is a duplicate, is senior, or is identical to one
already scored was the largest avoidable cost.

## Decision

Three zero-cost paths run in order before the model sees a posting:

1. **Dedupe** on `sha256(company | title | url)` against the database and
   within the run.
2. **Senior short circuit**: a title matching the senior regex (with the
   "member of technical staff" carve-out) is stored as excluded with a
   minimal record, so the exclusion is logged and the hash dedupes future
   runs, without a parse.
3. **Twin reuse**: an already scored row with the same company and title
   (case and whitespace insensitive) transfers its extraction and score.
   Scoring depends only on company, title, and requirements, so the
   transfer is exact. An excluded twin is not reused on the pipeline path
   because the exclusion may depend on the specific posting's location.

## Consequences

- The scheduled run typically makes a handful of model calls against
  thousands of postings seen.
- A twin inherits the first scored copy's rationale; rescoring updates all
  copies because rescoring reads stored extractions.
- The hash includes the url, so a url change is a new row by design.
