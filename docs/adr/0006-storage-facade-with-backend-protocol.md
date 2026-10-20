# 0006. Storage behind a facade with a backend protocol

Date: 2026-09-03
Status: accepted

## Context

The Snowflake backend was bolted on by rebinding the SQLite module's
functions at the bottom of the file when an environment variable was set.
It worked, but the two implementations drifted: Snowflake lacked columns
and the twin lookup that SQLite had, and nothing checked the signatures
matched.

## Decision

`sourcer.db` is a thin facade. It selects `sourcer.sqlitedb` or
`sourcer.snowdb` from `SOURCER_BACKEND` at import and re-exports their
functions. A `Backend` protocol declares the required functions and their
signatures; the selected module is annotated with that protocol so mypy
rejects a backend that drifts. A `Connection` protocol declares the
DB-API slice the rest of the package may use (`execute`, `executemany`,
`commit`, `close`).

Only the facade may import the backend modules; the layering test enforces
it.

## Consequences

- Adding a backend means implementing six functions and one connection
  wrapper, checked at type-check time.
- Every caller keeps importing `sourcer.db`; tests can reload the facade
  under a different environment to assert the switch.
- Row access is by column name on both backends, so SQL stays shared.
