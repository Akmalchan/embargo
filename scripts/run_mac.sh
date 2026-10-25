#!/usr/bin/env bash
# Laptop rehearsal: Ollama stands in for vLLM (same OpenAI-compatible API).
set -euo pipefail
export EG_LLM_URL="${EG_LLM_URL:-http://127.0.0.1:11434/v1}"
export EG_LLM_MODEL="${EG_LLM_MODEL:-qwen3-vl:4b-instruct}"
export EG_LLM_CONCURRENCY="${EG_LLM_CONCURRENCY:-2}"
[ -f data/vault/vault_index.csv ] || .venv/bin/python scripts/make_dataset.py
.venv/bin/uvicorn exportgate.webmail:app --host 127.0.0.1 --port 8791 &   # Kestrel Mail
.venv/bin/uvicorn exportgate.server:app --host 127.0.0.1 --port 8790
