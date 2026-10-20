# Maintainer guide

## Branch protection

The September 4, 2026 audit found no branch protection. On September 7,
GitHub rejected access to private branch protection and rulesets with an upgrade
requirement. No plan upgrade has been purchased. These are recommendations,
not enforced merge gates; passing workflow checks alone does not prevent a merge.

After the relevant checks exist and pass, configure a rule for `main`:

1. Require pull requests and resolution of review conversations.
2. Require validation against the latest base before merge. Use a merge queue
   only after workflows support its events.
3. Block force pushes and deletion of `main`.
4. Require one independent approval when another maintainer is available.
   A sole maintainer cannot approve their own PR; use an explicit documented
   exception or recruit a reviewer rather than creating an impossible gate.
5. Keep any emergency bypass narrow and document its use in the affected PR.
6. Prefer squash merges for focused changes. Delete merged topic branches only
   after confirming they contain no independent work.

Current check inventory: `lint, format, types, hygiene`, `tests (py3.12)`, `tests (py3.13)`, `tests (py3.14)`, `dependency audit`, and `package and images` from CI. The free Security workflow adds `security (python)` and `security (actions)`.

Check names can change. Select them from a recent run in GitHub, then confirm
that a failing check prevents merging. Do not advertise a coverage or security
gate until it is enforced. External publishing should depend on validation.

## Security scanner verification

Install the same isolated, hash-locked tools used in CI, then run both scans
and the synthetic tests. No account with a scanning vendor is needed.

```bash
python3.12 -m venv /tmp/sourcer-security-venv
/tmp/sourcer-security-venv/bin/python -m pip install --require-hashes -r security/requirements.txt
/tmp/sourcer-security-venv/bin/python security/test_scanners.py
/tmp/sourcer-security-venv/bin/bandit -r sourcer scripts security
/tmp/sourcer-security-venv/bin/zizmor --offline --min-severity=low .github/workflows
```

Refresh the scanner lock after reviewing dependency updates:

```bash
uv pip compile security/requirements.in --python-version 3.12 --generate-hashes -o security/requirements.txt
```

CI verifies this lock against its input and audits all scanner dependencies.
Informational workflow suggestions do not fail the security job. No Python
severity is filtered out. See [SECURITY.md](../SECURITY.md) for coverage limits.

## Change and release review

Use the commands in [CONTRIBUTING.md](../CONTRIBUTING.md). Review the final diff,
including generated output and licenses. Keep related tests and documentation
in the same change. Do not rewrite published history to tidy the changelog.

For a versioned package, use semantic versioning, reconcile the package version
with the changelog, and test the built distribution outside its checkout before
tagging. A successful wheel build alone is not a runtime check. For a website
or profile, record the deployed commit and use dated changelog entries; a
package release workflow is not necessary.

## Remaining work

Open: transactional recovery of a partly restored database, and signed
release artifacts. Neither is claimed by the current checks.
Handle vulnerability details through [SECURITY.md](../SECURITY.md).
