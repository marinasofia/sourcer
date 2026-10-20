# 0011. Public code repository

Date: 2026-10-19
Status: accepted, supersedes the code repository part of 0010

## Context

GitHub Actions minutes on a private repository are limited, and the pipeline
code holds no personal data: profiles, facts, results, and credentials already
live only in the private data repository.

## Decision

Publish the code as a public repository, started from one squashed commit so
that no earlier commit message or author detail carries over. Keep the data
repository private. The data workflow installs this code at a pinned commit and
never pushes into it.

## Consequences

CI runs without a minutes limit and rulesets protect `main`. Every change must
keep operator details out of code, comments, tests, commit messages, and pull
requests; the hygiene check and the security workflow run on every pull request.
