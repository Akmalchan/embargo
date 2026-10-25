#!/usr/bin/env bash
# One-time setup on the GB10 (or any Linux/Mac). Safe to run again.
#   bash scripts/bootstrap_box.sh            # set up + self-test
#   SHARE_SIZE=2000 bash scripts/bootstrap_box.sh
# Then start everything with:  bash scripts/run_gb10.sh
set -euo pipefail
cd "$(dirname "$0")/.."
say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

say "Machine"
uname -sm
(nproc 2>/dev/null || sysctl -n hw.ncpu) | sed 's/^/cores: /'
(free -g 2>/dev/null | sed -n 2p) || true
(nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>/dev/null) || echo "nvidia-smi: not found (fine on a laptop)"

say "Python environment"
if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found; installing (needs internet once)…"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
[ -d .venv ] || uv venv --python 3.12 .venv
if [ -d "${WHEELS:-/nonexistent}" ]; then
  uv pip install --python .venv/bin/python --no-index -f "$WHEELS" -r requirements.txt   # offline, from USB
else
  uv pip install --python .venv/bin/python -r requirements.txt
fi
PY=.venv/bin/python

say "Fictional dataset (Kestrel Precision)"
if [ ! -f data/vault/vault_index.csv ]; then
  $PY scripts/make_dataset.py --share-size "${SHARE_SIZE:-2000}"
else
  echo "already there: $(find data/share -type f \( -name '*.pdf' -o -name '*.png' -o -name '*.jpg' \) | wc -l | tr -d ' ') share files"
fi

say "Vault index + console pictures"
$PY -c "from exportgate.fingerprint import Vault; v=Vault.build(); print(len(v.items), 'originals indexed')"
$PY scripts/make_assets.py >/dev/null && $PY scripts/make_visuals.py >/dev/null && echo "pictures rendered"

say "Self-test"
$PY -m pytest -q -p no:warnings | tail -1
EG_READER=text $PY scripts/check.py | tail -1
EG_READER=text $PY scripts/eval.py --out RESULTS_no_model.md | tail -2

say "Local model"
if curl -sf "${EG_LLM_URL:-http://127.0.0.1:8000/v1}/models" >/dev/null; then
  echo "model server is up at ${EG_LLM_URL:-http://127.0.0.1:8000/v1}"
else
  echo "no model server yet. On the GB10:  bash scripts/start_vllm.sh   (see HANDOFF.md step 3)"
fi

say "Done"
echo "Start everything:   bash scripts/run_gb10.sh"
echo "Console:            http://127.0.0.1:8790   (from a laptop: ssh -L 8790:127.0.0.1:8790 <user>@<box-ip>)"
