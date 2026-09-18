# Project review — implementation status

The original review was followed by the requested implementation, with a baseline
snapshot and separate checkpoints. See [snapshot/publication notes](repository-preparation.md).

| Review item | Current status |
| --- | --- |
| Service layout | System `assistant.service` and `whisper-server.service`; helper and diagnostic scripts aligned; renderable example units supplied. |
| Credentials example | All imported fields supplied; optional missing settings fall back individually. Local credentials untracked. |
| Whisper defaults | Single `config.py` model setting shared by server launcher and fallback; `small.en` matches the inspected live service. |
| Cloud response refresh | Configurable 30–60 minute default; text-only cloud URL/model/key from config; validated atomic current/previous cache with seed fallback. |
| Generated response files | Historical project/associated batches snapshotted and consolidated; compatibility aliases point to the cache. |
| Acknowledgment order | Configurable before/after; publish failure or uncertainty produces a response. |
| Automation durability | SQLite jobs and reports, restart policy, persisted levels, restored sensor subscriptions, no blind replay of interrupted publication. |
| Music CLI | Private Unix IPC to the live player; no separate controller process. |
| Cloud fallback speech | Non-streamed fallback is spoken without repeating completed sentences. |
| SyncStreamer | Documentation aligned with actual v2 source; source unchanged. |
| Multiple voices/output paths | Preserved and documented, including named notification voice overrides. |
| Reproducible setup | Pinned Python dependencies, asset hashes, Whisper revision, service templates, bootstrap/check tools, and sanitized examples supplied. |
| Repository/history review | Current sensitive/runtime assets untracked but retained locally; credential-bearing old history preserved privately; source-only export supplied. |

## Operating documentation

- [README](../README.md): system purpose, flow, and components.
- [Setup](setup.md): dependencies, model provisioning, and service installation.
- [Operations](operations.md): configuration, durable jobs, voice selection, IPC, diagnostics.
- [SyncStreamer](../syncstreamer/README.md): exact implemented protocol and limitations.
- [Repository preparation](repository-preparation.md): snapshots, private history, clean export.
- [Validation](validation.md): tests, model checks, and activation status.

## Remaining deployment/release decisions

Activation completed on 18 September 2026, with service, MQTT, speaker registration,
HTTP, database, and music IPC checks passing. See [validation](validation.md).
Active defaults retain acknowledgment-before-action and skip overdue actions after
restart; both policies remain configurable in `config.py`. Pre-upgrade in-memory
jobs/context were not recoverable when the old process stopped.

A fully reproducible hardware release still needs frontend/speaker firmware revisions,
verification of the recorded Ollama model digest, model/sound redistribution review, and an owner-selected
project license. No history rewrite, public publication, or unrequested change to the
SyncStreamer source was performed.
