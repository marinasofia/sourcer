# Architecture decision records

One file per decision that is not obvious from the code. Each record states
the context, the decision, and the consequences, and is never edited after
acceptance; a later record supersedes it.

| ID | Title | Status |
|---|---|---|
| [0001](0001-split-public-and-private-repositories.md) | Split public code from private data | superseded by 0010 |
| [0002](0002-explicit-signal-exclusions.md) | Hard exclusions on explicit signals only | accepted |
| [0003](0003-hard-cost-cap.md) | A hard per-run cost cap checked before every model call | accepted |
| [0004](0004-single-parse-and-score-call.md) | One model call parses and scores a posting | superseded by 0012 |
| [0005](0005-zero-cost-paths-before-the-model.md) | Dedupe, senior short circuit, and twin reuse run before any model call | accepted |
| [0006](0006-storage-facade-with-backend-protocol.md) | Storage behind a facade with a backend protocol | accepted |
| [0007](0007-import-layering-as-a-test.md) | Import layering enforced by a test | accepted |
| [0008](0008-schema-validation-with-one-retry.md) | Schema-validated model replies with exactly one corrective retry | accepted |
| [0009](0009-package-layout-by-concern.md) | Package layout by concern | accepted |
| [0010](0010-private-single-operator-deployment.md) | Private single-operator deployment | accepted, code part superseded by 0011 |
| [0011](0011-public-code-repository.md) | Public code repository | accepted |
| [0012](0012-two-workers-extract-then-score.md) | Two workers read a posting: extract facts, then score fit | accepted |

Template for a new record:

```markdown
# NNNN. Title

Date: YYYY-MM-DD
Status: proposed | accepted | superseded by NNNN

## Context
## Decision
## Consequences
```
