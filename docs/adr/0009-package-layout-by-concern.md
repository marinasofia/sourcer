# 0009. Package layout by concern

Date: 2026-09-04
Status: accepted

## Context

ADR 0007 kept the package flat and noted that moving to packages later would
be a mechanical change guided by the layer table. The adapter list is about
to grow (Workable, SmartRecruiters, generic careers pages), application-side
tooling and a chat bot are planned, and the private repository reaches into
the package by module path, so any move has to keep old paths importable.

## Decision

Code is grouped by concern. `sources/` holds one adapter per ATS, each
exposing `fetch_postings(company)`, plus the SimplifyJobs lists and shared
helpers. `pipeline/` holds the nodes, the deterministic filters, the scoring
stage with the HTTP and MCP scoring service, and the graph. `apply/` holds
listing verification and reserved seats for tailoring and prefill. `bot/` and
`discovery/` are reserved packages. `probe/` discovers ATS boards and generic
careers pages. Configuration, storage,
the model client, entry points, and operator tools stay at the top level.

Code was moved, not rewritten. `sourcer.graph`, `sourcer.nodes`,
`sourcer.scoring`, and `sourcer.verify` remain as re-export shims; the
layering test asserts they carry no logic.

A `companies` table joins `postings` and `run_logs` in both backends and is
seeded from `companies.json` on every run. Its `submit_enabled` flag is private
operator state and is rejected in company input. New rows start disabled, and
removing a company disables its existing row.

## Consequences

- The layering test assigns layers by dotted module path and walks
  subpackages; every new module must be placed.
- New adapters register in `sourcer.sources.FETCHERS`; reserved stubs raise
  until they are wired.
- The `graph`, `scoring`, `verify` and `nodes` shims were deleted on
  2026-10-13, once nothing imported them.
