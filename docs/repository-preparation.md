# Public source export and private development snapshots

This public repository starts with fresh history from an audited source-only export.
The snapshots described below belong to the separate private development repository
and are not present in this public repository.

A private baseline snapshot was created before code changes:
`7d258ca`, tagged `snapshot/pre-reliability-20260917`.
Subsequent groups were committed separately, with `snapshot/before-*` tags before
response refresh, IPC/fallback, durable actions, packaging, and response-file migration.
In the private development checkout, inspect them with `git log --oneline --decorate`
and `git tag --list 'snapshot/*'`.

The baseline contains the user's pre-existing edits and historical generated files.
Associated response batches were also snapshotted before removal. This preserves a
local recovery path while keeping the working cache to current and previous versions.
Do not restore an entire old tree over the running service without a deployment plan.

## Sensitive history

The original repository already tracked `credentials.py` and compiled credential bytecode.
They have been removed from the current index, along with local registries, recordings,
logs, bytecode, and model binaries. Local assets/credentials remain on disk and are ignored.
**Private development commits and snapshot tags still contain private material.
Do not publish that separate development history.** No destructive history rewrite was performed.

Run `python3 tools/audit_repository.py` for a redacted filename/literal-secret check.
It deliberately never prints credential values. This targeted check is not a complete
secret-scanner guarantee. Review older revisions and rotate exposed credentials if
that history has ever been shared beyond its intended audience.

## Clean source export

After reviewing and committing the intended source tree:

```bash
python3 tools/export_source.py /tmp/dobby-source-export
```

The destination must be new and outside the live project. The explicit export rules
include source, tests, docs, templates, examples, and setup manifests, while excluding
Git history, credentials, private registries, generated state, recordings, binaries,
and symlinks. The export does not initialize a repository, commit, or publish anything.
Inspect the export before creating a new public repository. Customize default network
addresses and location-specific text if needed, and provision optional sound assets
separately.

No project license has yet been selected; none was invented or applied on the
owner's behalf. Third-party model, sound, and firmware terms need their own review.
A clean Python install can be verified automatically; a fully reproducible hardware
release additionally needs frontend/speaker revisions and verification of the recorded Ollama model digest.
