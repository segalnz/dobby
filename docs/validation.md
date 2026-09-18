# Validation record

Validated on 18 September 2026 against the local Linux/Python 3.12 deployment.

- A fresh `/tmp/dobby-clean-verify` virtual environment installed `requirements.lock` successfully; `pip check` found no broken dependencies.
- 18 isolated tests passed, covering configuration, credentials examples, virtualenv unit rendering, response validation/rotation, configured refresh endpoints, IPC to one shared player, stream fallback, both acknowledgment orders, MQTT publication timeouts, sensor re-subscription, persisted values, restart policies, interrupted execution, and durable failure reports.
- Main runtime modules imported successfully in the clean environment.
- A real text-only cloud refresh succeeded in a temporary cache, validating all nine categories and retaining exactly current/previous JSON files. It did not modify live responses or play audio.
- Rendered system units passed `systemd-analyze verify`; shell scripts passed syntax checks.
- Model asset sizes and SHA-256 hashes matched `deploy/assets.json`. The configured Ollama model's installed digest was recorded from its local API.
- SyncStreamer Python source is byte-for-byte unchanged from the baseline Git snapshot.
- Documentation links and fences passed checks. A source-only export excluded credentials, private registries, Git history, models, recordings, and runtime state.
- A targeted current/history audit printed filenames/counts only. The historical credential file and compiled credential bytecode remain in private Git history; that history must not be published.

## Activation

Activation completed through the user's terminal on 18 September 2026 at 02:11:42 UTC.
Original units are backed up under `state/deployment-backups/20260918T012929Z/`.
Post-activation read-only checks confirmed:

- `assistant.service` and `whisper-server.service` are active/running with zero restarts.
- Both use the intended virtualenv interpreter; Whisper launches through the shared-config wrapper and loaded `ggml-small.en.bin`.
- The pipeline reached “waiting for wake word”; MQTT control and notification clients connected successfully.
- A SyncStreamer speaker registered after restart.
- `jellyfin_cli.py --json status` reached the live controller and returned an idle player with an empty queue.
- The assistant status endpoint and Whisper HTTP endpoint both returned 200.
- SQLite `quick_check` returned `ok`; no pending jobs existed at verification time.
- The response cache contains only `current.json` and `previous.json`; no legacy generated template files remained at the project root.

The periodic refresh interval has not yet been observed through a complete live cycle;
the same refresh implementation passed the real cloud smoke test above.

No appliance actions or audible test playback were made. One normal cloud generation
request was used for the refresh smoke test; unit tests simulate cloud/device failures.
Passing tests do not prove
end-to-end microphone/speaker behavior.
