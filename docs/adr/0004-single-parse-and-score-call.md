# 0004. One model call parses and scores a posting

Date: 2026-08-24
Status: accepted

## Context

The original pipeline sent each posting to the model twice: once to
extract structured fields, once to score the extraction against the skills
vector. The second call re-sent most of the first call's output, so the
posting text was effectively paid for twice.

## Decision

The pipeline path uses one combined prompt (`PARSE_SCORE_SYSTEM`) whose
reply carries both the extracted fields and the score. The separate parse
and score prompts remain for the interactive surfaces (MCP tool, HTTP
service) where a caller may want the extraction alone, and for rescoring
stored extractions after the skills vector changes.

The prompt and the exact `messages.create` parameters are built by one
function shared with the Batches API path, so a batched posting is scored
identically to a synchronous one.

## Consequences

- Roughly half the per-posting cost.
- The score node performs no model call; it applies the exclusion rules
  and the timing flag to fields that arrived with the parse.
- Prompt changes must keep both the combined and the split prompts in
  agreement; the schema constants are composed from one definition.
