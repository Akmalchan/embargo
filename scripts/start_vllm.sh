#!/usr/bin/env bash
# Start the reader/agent model on the GB10. Local only: binds 127.0.0.1 unless EG_BIND says otherwise.
# Source: NVIDIA local-ai-inference-recipes (Qwen3.6-35B-A3B NVFP4 on DGX Spark, image nvcr.io/nvidia/vllm:26.10.pre1-py3).
# [verify on the box] model id, image tag, and whether the NVFP4 build accepts images.
set -euo pipefail
MODEL="${EG_LLM_MODEL:-nvidia/Qwen3.6-35B-A3B-NVFP4}"
IMAGE="${VLLM_IMAGE:-nvcr.io/nvidia/vllm:26.10.pre1-py3}"
UTIL="${VLLM_UTIL:-0.55}"            # ~66 GiB incl. KV for batching; leaves room for everything else
BIND="${EG_BIND:-127.0.0.1}"         # use 0.0.0.0 only if the OpenShell sandbox must reach it via host.openshell.internal
HF_CACHE="${HF_CACHE:-$HOME/.cache/huggingface}"   # pre-load weights here from the USB stick

sudo sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches' || true
docker run -d --name eg-vllm --gpus all --ipc=host --network host \
  -v "$HF_CACHE":/root/.cache/huggingface -e HF_HUB_OFFLINE=1 \
  "$IMAGE" vllm serve "$MODEL" \
    --host "$BIND" --port 8000 \
    --gpu-memory-utilization "$UTIL" \
    --max-model-len 32768 --max-num-seqs 32 \
    --kv-cache-dtype fp8 \
    --limit-mm-per-prompt '{"image":1,"video":0}' \
    --enable-auto-tool-choice --tool-call-parser qwen3_coder --reasoning-parser qwen3   # [verify] parser names in recipe.yaml; NemoClaw onboard tests tool calling
echo "waiting for vLLM…"; until curl -sf "http://127.0.0.1:8000/v1/models" >/dev/null; do sleep 5; done
curl -s http://127.0.0.1:8000/v1/models | head -c 300; echo
