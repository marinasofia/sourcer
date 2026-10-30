# 0014. Skill weights are computed from approved facts

Date: 2026-10-30
Status: accepted

## Context

Onboarding (0013) gives a candidate approved facts, but the skills vector
still needed weights typed by hand. Typed weights are slow, drift from the
facts, and say nothing about why one skill outranks another. The profile also
has to keep up as the candidate's sources change.

## Decision

1. `onboard/weights.py` computes one weight per skill named by approved facts,
   with a fixed formula and no model call:
   `score = 0.40 depth + 0.20 recency + 0.15 agreement + 0.25 demand` and
   `weight = round(1 + 9 score)`.
   - depth: distinct jobs, projects and other groups naming the skill, full
     at 5, scaled to 0.8, plus 0.2 when a fact says the work shipped
     (deployed, in production, users, released).
   - recency: the latest date among those facts: 1 within 6 months, 0.75
     within a year, 0.5 within two, 0.25 within four, else 0. Undated is 0.
   - agreement: distinct source types naming it, full at 3.
   - demand: the share of postings in the local SQLite database whose title
     fits the target roles (`config.ROLE_RE`) and whose text names the skill
     or a synonym, full at 40%. With fewer than 30 such postings the term is
     skipped, the other three are divided by 0.75, and the reason says so.
2. Skill names are normalized by code: case, spacing and a short alias table
   (Postgres and PostgreSQL, k8s and Kubernetes). A model call to normalize
   names was allowed but is not used: the table is deterministic, free and
   testable.
3. Each skill keeps a one line reason built from the same numbers, such as
   "2 projects and 1 job with shipped work, used this month, named by resume
   and GitHub, in 41% of matching postings".
4. skills_vector.json keeps its schema (`skill`, `weight`, `category`,
   `synonyms`) and adds `reason` and `source` (`computed` or `override`). The
   scorer drops those two keys before they reach a prompt.
5. `sourcer skills set` stores an override that every recompute keeps until
   `sourcer skills reset`. A vector with entries Sourcer did not write is
   treated as hand written and never replaced without `--force`, which keeps
   a `.bak` copy.
6. Weights are recomputed after `sourcer facts review`, after `sourcer sync`
   and after `sourcer forget`. The summary after a scan ranks skills with
   the same formula over approved and newly found facts, without writing.
7. `sourcer sync` keeps the profile current: collectors stamp what they read
   (a file hash, a repo's last push) and a stamp is saved only once its items
   were read, so a declined or failed run reads them again.

## Consequences

- No one types a weight, every weight can be explained, and the same facts
  and postings always give the same weights.
- With evidence scoring on, fit is judged from the facts; the vector matters
  when evidence scoring is off and as a readable summary.
- Demand needs a local database with enough postings; a new user sees it
  skipped until the first runs fill it. Snowflake storage is not read.
- The weights in the formula are judgment calls. Changing them changes every
  computed weight and so the scoring prompt when evidence scoring is off.
