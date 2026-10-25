#!/usr/bin/env bash
# Everything on the GB10, in order. Run from the repo root.
set -euo pipefail
export NEMOCLAW_DISABLE_TELEMETRY=1
export EG_LLM_URL="${EG_LLM_URL:-http://127.0.0.1:8000/v1}"
export EG_LLM_MODEL="${EG_LLM_MODEL:-nvidia/Qwen3.6-35B-A3B-NVFP4}"
export EG_LLM_CONCURRENCY="${EG_LLM_CONCURRENCY:-32}"
export EG_SWEEP_WORKERS="${EG_SWEEP_WORKERS:-16}"
export EG_READER="${EG_READER:-auto}"

# 0. python env (wheels from the USB stick if offline: uv pip install --no-index -f /media/usb/wheels ...)
[ -d .venv ] || { uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt; }

# 1. model
curl -sf "$EG_LLM_URL/models" >/dev/null || bash scripts/start_vllm.sh

# 2. NemoClaw agent on the same model (custom provider). [verify] flags on the installed NemoClaw version.
#    NEMOCLAW_PROVIDER=custom NEMOCLAW_ENDPOINT_URL=http://localhost:8000/v1 NEMOCLAW_AUTH=none nemoclaw onboard
#    then: nemoclaw my-assistant policy-add --from-file policies/embargo-egress.yaml --yes
#    and copy openclaw/skills/embargo into the agent's skills folder.

# 3. data + vault (only first time)
[ -f data/vault/vault_index.csv ] || .venv/bin/python scripts/make_dataset.py --share-size "${SHARE_SIZE:-2000}"

# 4. dashboard/API (0.0.0.0 only if the sandboxed agent must reach it)
.venv/bin/uvicorn exportgate.server:app --host "${EG_API_BIND:-127.0.0.1}" --port 8790 &   # also starts the drop-folder watcher
# 4b. Kestrel Mail, the company webmail that sends through Embargo (http://mail.kestrel.localhost:8791)
.venv/bin/uvicorn exportgate.webmail:app --host 127.0.0.1 --port 8791 &
# 5. Slack bot (tokens from env, never from files)
if [ -n "${SLACK_BOT_TOKEN:-}" ] && [ -n "${SLACK_APP_TOKEN:-}" ]; then .venv/bin/python -m exportgate.slack_app & fi
wait
