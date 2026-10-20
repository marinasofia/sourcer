# Branch protection for `main`

Rulesets are free on a public repository. Apply the settings below to `main`;
the equivalent `gh` command is below.

| Rule | Setting | Why |
|---|---|---|
| Restrict deletions | on | protect the private code history |
| Require linear history | on | squash merges keep the changelog readable |
| Require a pull request before merging | on, dismiss stale approvals | require an independent approval when another maintainer is available |
| Require review from Code Owners | when an independent reviewer is available | a sole maintainer cannot approve their own PR |
| Require status checks to pass | on, require branches to be up to date | the checks below are the gate |
| Block force pushes | on | protect reviewed history |
| Require signed commits | recommended | cheap once a key is configured |
| Bypass list | none | the scheduler has read-only code access and needs no bypass |

## Required status checks

Names must match the job names in `.github/workflows/ci.yml` and
`security.yml` exactly:

- `lint, format, types, hygiene`
- `tests (py3.12)`
- `tests (py3.13)`
- `tests (py3.14)`
- `dependency audit`
- `package and images`
- `security (python)`
- `security (actions)`

## Scheduler access

The scheduler reads a pinned code commit through a read-only deploy key. It
saves results only in the private data repository. Pages deployment and the
former write-capable code deploy key have been removed.

## `gh` equivalent

```bash
gh api -X POST repos/marinasofia/sourcer/rulesets --input - <<'JSON'
{
  "name": "main",
  "target": "branch",
  "enforcement": "active",
  "conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}},
  "rules": [
    {"type": "deletion"},
    {"type": "non_fast_forward"},
    {"type": "required_linear_history"},
    {"type": "pull_request", "parameters": {
      "required_approving_review_count": 0,
      "dismiss_stale_reviews_on_push": true,
      "require_code_owner_review": false,
      "require_last_push_approval": false,
      "required_review_thread_resolution": true}},
    {"type": "required_status_checks", "parameters": {
      "strict_required_status_checks_policy": true,
      "required_status_checks": [
        {"context": "lint, format, types, hygiene"},
        {"context": "tests (py3.12)"},
        {"context": "tests (py3.13)"},
        {"context": "tests (py3.14)"},
        {"context": "dependency audit"},
        {"context": "package and images"},
        {"context": "security (python)"},
        {"context": "security (actions)"}]}}
  ]
}
JSON
```

Enable Dependabot alerts and security updates where available. Use the reporting
route in SECURITY.md. Do not purchase an upgrade or make the repository public
to enable a security feature.
