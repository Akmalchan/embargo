#!/usr/bin/env bash
# Fast path on a borrowed GB10: find whatever model server is already there, install, self-test, start.
#   bash scripts/gb10_quick.sh          # then open http://127.0.0.1:8790 and http://127.0.0.1:8791
# Never downloads a model on its own. If no model server is running, it runs with EG_READER=text
# (fingerprint + register + PDF text), which still decides every demo moment.
set -uo pipefail
cd "$(dirname "$0")/.."
say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

say "Machine"
uname -sm; nproc | sed 's/^/cores: /'; free -g | sed -n 2p
nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader 2>/dev/null || echo "nvidia-smi: not found"
for t in nemoclaw openshell openclaw ollama docker vllm uv; do printf '%-10s %s\n' "$t" "$(command -v $t || echo -)"; done

say "Model server already running?"
if M=$(curl -sf -m 3 http://127.0.0.1:8000/v1/models); then
  export EG_LLM_URL=http://127.0.0.1:8000/v1
  export EG_LLM_MODEL=$(printf '%s' "$M" | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"][0]["id"])')
  export EG_READER=auto; echo "vLLM/OpenAI-compatible on :8000 -> $EG_LLM_MODEL"
elif T=$(curl -sf -m 3 http://127.0.0.1:11434/api/tags); then
  export EG_LLM_URL=http://127.0.0.1:11434/v1
  export EG_LLM_MODEL=$(printf '%s' "$T" | python3 -c 'import json,sys; n=[m["name"] for m in json.load(sys.stdin)["models"]]; v=[x for x in n if "vl" in x.lower()]; i=[x for x in v if "instruct" in x]; print((i or v or n or [""])[0])')
  if [ -n "$EG_LLM_MODEL" ]; then export EG_READER=auto; echo "Ollama on :11434 -> $EG_LLM_MODEL"; else export EG_READER=text; echo "Ollama has no models -> text mode"; fi
else
  export EG_READER=text; echo "no model server -> EG_READER=text (rules + fingerprint only)"
fi

say "Python environment"
command -v uv >/dev/null || { curl -LsSf https://astral.sh/uv/install.sh | sh; export PATH="$HOME/.local/bin:$PATH"; }
[ -d .venv ] || uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -q -r requirements.txt || { echo "install failed"; exit 1; }
PY=.venv/bin/python

say "Self-test"
$PY -m pytest -q tests 2>&1 | grep -E "passed|failed|error" | tail -1
EG_READER=text $PY scripts/check.py 2>&1 | grep -E "checks passed" | tail -1

say "Start console (8790, + SMTP 2525) and Kestrel Mail (8791)"
pkill -f "uvicorn exportgate" 2>/dev/null; sleep 2; pkill -9 -f "uvicorn exportgate" 2>/dev/null; sleep 1
nohup .venv/bin/uvicorn exportgate.server:app --host 127.0.0.1 --port 8790 > /tmp/embargo_console.log 2>&1 &
nohup .venv/bin/uvicorn exportgate.webmail:app --host 127.0.0.1 --port 8791 > /tmp/embargo_mail.log 2>&1 &
sleep 4
curl -sf -o /dev/null http://127.0.0.1:8790/ && echo "console  http://127.0.0.1:8790  ok" || echo "console failed: tail /tmp/embargo_console.log"
curl -sf -o /dev/null http://127.0.0.1:8791/ && echo "mail     http://127.0.0.1:8791  ok" || echo "mail failed: tail /tmp/embargo_mail.log"
curl -s -X POST http://127.0.0.1:8790/api/demo/reset >/dev/null && echo "demo data reset"
bash scripts/make_desktop_folder.sh >/dev/null 2>&1 && echo "~/Desktop/Kestrel Drawings ready"
printf '\nreader=%s  model=%s  url=%s\n' "$EG_READER" "${EG_LLM_MODEL:-none}" "${EG_LLM_URL:-none}"
