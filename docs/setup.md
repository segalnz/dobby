# Setup and deployment

This procedure captures the reviewed Python 3.12 Linux installation. Dependency pins,
model hashes, and a Whisper source revision are supplied. Hardware firmware and custom
voice/sound assets require provisioning; this is not a hardware-tested universal installer.

## Python environment

Install a Python 3.12 interpreter with venv support. Then, from the project directory:

```bash
bash tools/bootstrap.sh
.venv/bin/python -m pip check
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -v
```

`requirements.lock` captures exact installed package versions, including optional Piper
support. The bootstrap script creates `.venv` and installs those pins; it does not start
services. Native package requirements depend on the target platform. Building Whisper
requires a C/C++ compiler, CMake, Git, and the libraries selected by its build options.

## Model and firmware assets

`deploy/assets.json` records the reviewed Whisper commit and SHA-256/size of required
Whisper and Kokoro assets. To reproduce them, copy the model files from a trusted backup
or obtain the same upstream artifacts and verify their hashes:

```bash
python3 tools/check_assets.py
```

Expected paths are `whisper/models/ggml-small.en.bin`,
`models/kokoro/kokoro-v1.0.onnx`, and `models/kokoro/voices-v1.0.bin`.
The recorded Whisper checkout was clean. Clone the repository listed in the manifest,
check out its exact revision, and build its `whisper-server` and `whisper-cli` targets:

```bash
git clone https://github.com/ggml-org/whisper.cpp.git whisper
git -C whisper checkout 95ea8f9bfb03a15db08a8989966fd1ae3361e20d
cmake -S whisper -B whisper/build -DCMAKE_BUILD_TYPE=Release
cmake --build whisper/build --config Release --target whisper-server whisper-cli -j
```

The checkout's `models/download-ggml-model.sh small.en` can obtain the Whisper model;
check it against the manifest after downloading. For Kokoro, use the exact filenames
and hashes recorded above; this setup does not silently download an unverified replacement.
Keep required sound effects in `sounds/`, and provision the Piper model configured by
`PIPER_MODEL_PATH` if that separate integration is used. Check redistribution licenses.

Provision the `qwen2.5:1.5b` model in Ollama. The exact installed model digest is recorded in `deploy/assets.json`; compare it
with the new server's `/api/tags` response before using a potentially changed tag. Deploy frontend firmware and matching SyncStreamer
v2 speaker firmware separately. Their exact source revisions were not available in
this server checkout; record them before publishing a complete hardware release.

## Local configuration

On a fresh export, create local configuration files (do not overwrite an existing installation):

```bash
cp credentials.example.py credentials.py
cp device_registry.example.json device_registry.json
cp sensor_registry.example.json sensor_registry.json
```

Fill in your endpoints and credentials, MQTT topics/actions, and sensor mappings.
Review `config.py`, including the Jellyfin URL and installation-specific frontend/default
addresses. Set `WHISPER_MODEL` there if choosing another model, and update the asset
manifest deliberately. The current model setting is shared by server and fallback.

Configure external Ollama, MQTT, Jellyfin, and OpenClaw services. The reviewed OpenClaw
version was `2026.4.12`, configured with `deepseek/deepseek-chat`, gateway port 18789,
and the chat-completions endpoint enabled. Its credentials/workspace are private deployment
state and are not included. Use the same Unix user for OpenClaw and the assistant so
agent music commands can access the private IPC socket.

## Render and install system units

Render templates for the actual user/project/interpreter; do not copy the `.in` files
straight into systemd:

```bash
.venv/bin/python deploy/render_units.py --python "$PWD/.venv/bin/python" --output /tmp/dobby-units
systemd-analyze verify /tmp/dobby-units/assistant.service /tmp/dobby-units/whisper-server.service
```

For an existing installation, first save the existing unit files and arrange an
interruption window. Review the rendered units, then install and activate them:

```bash
sudo install -m 0644 /tmp/dobby-units/assistant.service /etc/systemd/system/assistant.service
sudo install -m 0644 /tmp/dobby-units/whisper-server.service /etc/systemd/system/whisper-server.service
sudo systemctl daemon-reload
sudo systemctl enable whisper-server.service assistant.service
sudo systemctl restart whisper-server.service assistant.service
systemctl status whisper-server.service assistant.service
```

The templates use normal systemd process ownership and do not use a broad `pkill` in
`ExecStartPre`. Existing OpenClaw configuration/service need not be replaced. The original
service entrypoints remain compatible with the updated code and launcher.

Check logs, Whisper arguments, speaker registration, and music IPC status. Live device,
notification, voice, and cloud checks should be done deliberately: they can operate
appliances, play audio, and incur provider charges.

See [operations](operations.md) for refresh intervals, acknowledgment order, restart
policies, voice selection, and live diagnostic tools.
