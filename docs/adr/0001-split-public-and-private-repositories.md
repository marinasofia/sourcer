# 0001. Split public code from private data

Date: 2026-08-24
Status: accepted

## Context

Sourcer is both a portfolio piece that recruiters read and a personal tool
whose outputs (a weighted skills vector, per-posting scores, skill gaps,
resumes) are private. A single repository would either hide the code or
publish the data.

## Decision

Two repositories. The public one holds all pipeline code, tests, deployment
surfaces, and an aggregate-only dashboard. A private one holds the skills
vector, profile, resumes, application kits, the full `data.json` export, and
the scheduled workflow. The private workflow checks out the public
repository for code, runs it against private state, and pushes back exactly
two files: `docs/data-public.json` (run totals) and `docs/showcase.json`
(sector-labelled, name-redacted, strengths-only blurbs).

The public repository is the single source of truth for code. The private
repository does not carry a copy of the package.

## Consequences

- The public history never contained personal data, so it can stay public
  without a rewrite.
- The showcase exporter must fail closed: a leak scan rejects any blurb with
  gap vocabulary or an employer name, and the pre-commit and CI hygiene
  check refuses private file paths.
- Anything private-only (application kits, gap reports, the local board UI)
  lives in the private repository and imports the public package.
