# 0002. Hard exclusions on explicit signals only

Date: 2026-08-23
Status: accepted

## Context

Early versions filtered any posting whose timing or level looked wrong,
which silently dropped roles whose descriptions simply did not state a
start date or employment type. Recall matters more than precision for a
job search: a false exclusion is a lost opportunity, a false inclusion
costs a glance.

## Decision

A posting is hard excluded (score 0, reason logged) only on explicit
signals: a senior-level title, five or more years required, full time with
an immediate start, a contract role (unless it is full time and starts in the
target summer), hybrid or onsite work outside NYC, or a full time start before
graduation when a candidate profile is present.
Anything ambiguous is scored normally and flagged `timing_unclear` with a
note of what is missing.

Rules live in `sourcer.eligibility` as a table, evaluated in order, first
failure wins. Each rule is a pure function of the posting and the optional
profile, returning a `Verdict` with a stable rule code, the reason, and the
posting's own words when it read them. `eligible()` is the one entry point:
`EXCLUSION_RULES` apply when a posting is scored, `BOARD_RULES` add the stored
location and the level grade for rows already on the board.

## Consequences

- Ambiguous postings show up in the digest with a flag rather than
  vanishing.
- Adding a rule means adding one function and one table entry, plus tests.
- The candidate profile must be passed in; rules never read files.
