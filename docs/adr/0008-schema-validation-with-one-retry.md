# 0008. Schema-validated model replies with exactly one corrective retry

Date: 2026-08-23
Status: accepted

## Context

Model replies are untrusted input. A reply that is not JSON, or is JSON
missing a field, must not reach the database, and a validation loop must
not spend without bound.

## Decision

Every model call goes through `llm.json_call`. The reply is unfenced,
parsed, and checked against a schema of required keys and allowed types.
On failure the call sends the model its own reply plus a corrective nudge
naming the error, exactly once. A second failure returns the error to the
caller, which logs it in `parse_failures` and moves on. There is no third
attempt.

Board-provided fields (title, location, url) always override the model's
echo of them after a successful parse.

## Consequences

- Worst case cost per posting is two calls.
- Schema failures are visible in the run log and the digest; the public
  dashboard publishes the rate.
- Prompt injection inside a posting can at most produce an invalid or
  oddly scored reply; it cannot change which posting is stored under which
  url, and no model output is executed or interpolated into SQL.
