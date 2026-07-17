#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/deploy.sh [--runtime auto|gpu|cpu] [--timeout-seconds N]

auto (default) uses the GPU profile only when Docker can start the configured
TEI GPU image. Otherwise it deploys the Qwen CPU runtime with the same model
and vector dimension. EMBEDDING_MODE=cpu|gpu remains supported for compatibility.
EOF
}

runtime="${DEPLOY_RUNTIME:-${EMBEDDING_MODE:-auto}}"
timeout_seconds="${DEPLOY_TIMEOUT_SECONDS:-900}"
runtime_explicit=false
timeout_explicit=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --runtime)
      runtime="$2"
      runtime_explicit=true
      shift 2
      ;;
    --timeout-seconds)
      timeout_seconds="$2"
      timeout_explicit=true
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

project_name="${COMPOSE_PROJECT_NAME:-knowledge-kb}"
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required. Install Docker Engine/Desktop before deploying." >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose v2 is required." >&2
  exit 1
fi

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example. Review secrets before production use."
fi

dotenv_value() {
  local key="$1"
  awk -F= -v key="$key" '$1 == key {sub("^[^=]*=", ""); value=$0} END {print value}' .env
}

if [[ "$runtime_explicit" == false && -z "${DEPLOY_RUNTIME:-}" && -z "${EMBEDDING_MODE:-}" ]]; then
  configured_runtime="$(dotenv_value DEPLOY_RUNTIME)"
  [[ -z "$configured_runtime" ]] && configured_runtime="$(dotenv_value EMBEDDING_MODE)"
  [[ -n "$configured_runtime" ]] && runtime="$configured_runtime"
fi

if [[ "$timeout_explicit" == false && -z "${DEPLOY_TIMEOUT_SECONDS:-}" ]]; then
  configured_timeout="$(dotenv_value DEPLOY_TIMEOUT_SECONDS)"
  [[ -n "$configured_timeout" ]] && timeout_seconds="$configured_timeout"
fi

tei_gpu_image="${TEI_GPU_IMAGE:-}"
if [[ -z "$tei_gpu_image" ]]; then
  tei_gpu_image="$(dotenv_value TEI_GPU_IMAGE)"
fi
tei_gpu_image="${tei_gpu_image:-ghcr.io/huggingface/text-embeddings-inference:cuda-1.8.3}"

if [[ "$runtime" != "auto" && "$runtime" != "gpu" && "$runtime" != "cpu" ]]; then
  echo "DEPLOY_RUNTIME or EMBEDDING_MODE must be auto, gpu, or cpu" >&2
  exit 2
fi

gpu_available() {
  docker run --rm --gpus all --entrypoint /bin/sh "$tei_gpu_image" -c 'exit 0' >/dev/null 2>&1
}

selected_runtime="$runtime"
if [[ "$runtime" == "auto" ]]; then
  if gpu_available; then
    selected_runtime="gpu"
  else
    selected_runtime="cpu"
  fi
elif [[ "$runtime" == "gpu" ]]; then
  if ! docker run --rm --gpus all --entrypoint /bin/sh "$tei_gpu_image" -c 'exit 0'; then
    echo "The configured GPU profile is incompatible with this Docker/NVIDIA runtime." >&2
    exit 1
  fi
fi

if [[ "$selected_runtime" == "gpu" ]]; then
  override_file="docker-compose.embedding-gpu.yml"
  export TEI_GPU_IMAGE="$tei_gpu_image"
else
  override_file="docker-compose.embedding-cpu.yml"
fi

compose=(docker compose -p "$project_name" -f docker-compose.yml -f "$override_file")
echo "Selected runtime: $selected_runtime"
"${compose[@]}" up -d --build

deadline=$((SECONDS + timeout_seconds))
until "${compose[@]}" exec -T backend python -m app.scripts.smoke_embedding >/dev/null 2>&1; do
  if (( SECONDS >= deadline )); then
    echo "Embedding smoke test timed out after ${timeout_seconds}s." >&2
    "${compose[@]}" ps >&2 || true
    "${compose[@]}" logs --tail 100 embedding-qwen backend >&2 || true
    exit 1
  fi
  sleep 2
done

"${compose[@]}" ps
echo "Deployment completed with runtime: $selected_runtime"
