# 0010. Private single-operator deployment

Date: 2026-09-04
Status: accepted, supersedes 0001; the code repository part is superseded by 0011

## Context

The system now handles personal profile data, application materials, private
Telegram credentials, preference feedback, and scheduled job-search activity.
An aggregate public dashboard adds exposure without helping the core workflow.

## Decision

Keep both the code and data repositories private. Store personal state only in
the data repository. The data workflow may read the code repository through a
deploy key, but it must never push generated results into the code repository.
Disable GitHub Pages and remove committed dashboard output.

Telegram remains disabled by default. Network activity requires a token, an
allowlisted chat, an allowlisted user, and an explicit dry-run opt-out.

## Consequences

The former public dashboard is retired. Aggregate export code remains available
for local use, but no automation publishes its output. Repository collaborators
and deploy keys become part of the trusted boundary and need periodic review.
