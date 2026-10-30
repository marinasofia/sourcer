# 0012. Two workers read a posting: extract facts, then score fit

Date: 2026-10-27
Status: accepted, supersedes 0004

## Context

Under 0004 one combined call extracted a posting's fields and scored it in
the same reply. The rules (start date, location, seniority, years required)
then ran on fields the scoring model had already paid to produce, and a
posting that a rule would exclude had still cost a full scoring call. The
fields also arrived without the posting's own words, so a rule could act on
a value the model inferred rather than one the posting stated, and nothing
in code could tell the two apart.

The rules needed checked facts, each with a quote from the posting, before
any scoring spend.

## Decision

Each posting is read by two workers in sequence.

1. The extraction worker (`llm.extract_posting`, model `config.EXTRACT_MODEL`,
   `SOURCER_EXTRACT_MODEL`) copies the posting's facts: start date, place,
   work mode, seniority and minimum years, each with the posting's own words.
   `verify_extract` keeps only a fact whose quote appears in the posting and
   supports the value; anything else is reset to unknown.
2. The rules (`eligibility.eligible`) run on the checked facts. A posting
   that fails stops here with the rule's reason and the quote.
3. Only a posting that passes is sent to the scoring worker
   (`llm.score_posting`, model `config.MODEL` or `SOURCER_SCORE_MODEL`),
   which answers the fixed fit checks against the candidate's evidence.

`parse_and_score` wires the three steps and records both models and the
checked facts in the score trace.

## Consequences

- Two model calls per scored posting instead of one, but the extraction call
  is small and runs on a cheaper model, and an excluded posting never costs
  a scoring call.
- A rule can be explained with the posting's words, and a fact the model
  inferred without a quote never reaches a rule.
- `MODEL_PRICES` must know both default model ids or the cost cap prices the
  extraction call at the fallback rates.
- 0004 is superseded. The combined `PARSE_SCORE_SYSTEM` prompt and its schema
  remain for callers that still read them, but the pipeline no longer uses
  them for a single combined call.
