# 0007. Import layering enforced by a test

Date: 2026-09-03
Status: accepted

## Context

The package is flat: two dozen modules in one directory. That is the right
size for the project, and a package-per-layer restructure would churn every
import for no functional gain. The risk of a flat package is that
dependency direction erodes: an entry point gets imported by the pipeline,
a storage backend reaches into the model helpers.

## Decision

Modules are assigned to four layers in `tests/test_architecture.py`:

| Layer | Modules |
|---|---|
| 0, configuration | `config`, `log` |
| 1, adapters | `llm`, `sources`, `sqlitedb`, `snowdb`, `db` |
| 2, pipeline | `pipeline`, `digest` (the original flat modules have since moved into packages, see ADR 0009) |
| 3, entry points and tools | everything else |

A module may import from its own layer or below. Every module must be
assigned. Backend modules may be imported only by the facade. The test
parses imports with `ast`, so it needs no third-party tool and runs in the
normal suite.

## Consequences

- Adding a module fails the suite until it is placed in a layer, which is
  the moment to think about where it belongs.
- Moving to packages later is a mechanical change guided by the table.
