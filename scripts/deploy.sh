#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/deploy.sh [--database-mode auto|local|cloud] [--runtime auto|gpu|cpu] [--timeout-seconds N]

Database auto mode selects cloud only when DATABASE_URL is configured.
Embedding auto mode prefers a compatible NVIDIA GPU and otherwise uses the
Qwen CPU service with the same model and 1024-dimensional vector contract.
EOF
}

database_mode="${DEPLOY_DATABASE_MODE:-auto}"
runtime="${DEPLOY_RUNTIME:-${EMBEDDING_MODE:-auto}}"
timeout_seconds="${DEPLOY_TIMEOUT_SECONDS:-900}"
database_mode_explicit=false
runtime_explicit=false
timeout_explicit=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --database-mode)
      database_mode="$2"
      database_mode_explicit=true
      shift 2
      ;;
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

if ! command -v timeout >/dev/null 2>&1; then
  echo "GNU timeout (coreutils) is required to enforce deployment time limits." >&2
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

configured_value() {
  local key="$1"
  local value="${!key:-}"
  if [[ -z "$value" ]]; then
    value="$(dotenv_value "$key")"
  fi
  printf '%s' "$value"
}

if [[ "$database_mode_explicit" == false && -z "${DEPLOY_DATABASE_MODE:-}" ]]; then
  configured_database_mode="$(dotenv_value DEPLOY_DATABASE_MODE)"
  [[ -n "$configured_database_mode" ]] && database_mode="$configured_database_mode"
fi

if [[ "$runtime_explicit" == false && -z "${DEPLOY_RUNTIME:-}" && -z "${EMBEDDING_MODE:-}" ]]; then
  configured_runtime="$(dotenv_value DEPLOY_RUNTIME)"
  [[ -z "$configured_runtime" ]] && configured_runtime="$(dotenv_value EMBEDDING_MODE)"
  [[ -n "$configured_runtime" ]] && runtime="$configured_runtime"
fi

if [[ "$timeout_explicit" == false && -z "${DEPLOY_TIMEOUT_SECONDS:-}" ]]; then
  configured_timeout="$(dotenv_value DEPLOY_TIMEOUT_SECONDS)"
  [[ -n "$configured_timeout" ]] && timeout_seconds="$configured_timeout"
fi

if [[ "$database_mode" != "auto" && "$database_mode" != "local" && "$database_mode" != "cloud" ]]; then
  echo "DEPLOY_DATABASE_MODE must be auto, local, or cloud" >&2
  exit 2
fi

if [[ "$runtime" != "auto" && "$runtime" != "gpu" && "$runtime" != "cpu" ]]; then
  echo "DEPLOY_RUNTIME or EMBEDDING_MODE must be auto, gpu, or cpu" >&2
  exit 2
fi
if [[ ! "$timeout_seconds" =~ ^[1-9][0-9]*$ ]]; then
  echo "DEPLOY_TIMEOUT_SECONDS must be a positive integer" >&2
  exit 2
fi

database_url="$(configured_value DATABASE_URL)"
if [[ "$database_mode" == "auto" ]]; then
  if [[ -n "$database_url" ]]; then
    database_mode="cloud"
  else
    database_mode="local"
  fi
fi

if [[ "$database_mode" == "cloud" ]]; then
  if [[ -z "$database_url" || "$database_url" == *"replace-with"* || "$database_url" == *"db.example.com"* ]]; then
    echo "Set a real DATABASE_URL when DEPLOY_DATABASE_MODE=cloud." >&2
    exit 1
  fi
  if [[ ! "$database_url" =~ ^(postgres|postgresql|postgresql\+psycopg2):// ]]; then
    echo "DATABASE_URL must use a PostgreSQL connection scheme." >&2
    exit 1
  fi
  if [[ "$(configured_value MEDIA_STORAGE_BACKEND)" != "s3" ]]; then
    echo "MEDIA_STORAGE_BACKEND=s3 is required in cloud database mode." >&2
    exit 1
  fi
  s3_bucket="$(configured_value S3_BUCKET)"
  if [[ -z "$s3_bucket" || "$s3_bucket" == *"replace-with"* ]]; then
    echo "Set a real S3_BUCKET in cloud database mode." >&2
    exit 1
  fi
  s3_access_key="$(configured_value S3_ACCESS_KEY_ID)"
  s3_secret_key="$(configured_value S3_SECRET_ACCESS_KEY)"
  if [[ -n "$s3_access_key" && -z "$s3_secret_key" ]] || [[ -z "$s3_access_key" && -n "$s3_secret_key" ]]; then
    echo "S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY must be set together." >&2
    exit 1
  fi
  if [[ -n "$(configured_value S3_SESSION_TOKEN)" && -z "$s3_access_key" ]]; then
    echo "S3_SESSION_TOKEN requires S3 access key credentials." >&2
    exit 1
  fi
  s3_endpoint="$(configured_value S3_ENDPOINT_URL)"
  if [[ -n "$s3_endpoint" && ! "$s3_endpoint" =~ ^https?:// ]]; then
    echo "S3_ENDPOINT_URL must start with http:// or https://." >&2
    exit 1
  fi
  admin_username="$(configured_value INITIAL_ADMIN_USERNAME)"
  admin_password="$(configured_value INITIAL_ADMIN_PASSWORD)"
  if [[ -n "$admin_username" && -z "$admin_password" ]] || [[ -z "$admin_username" && -n "$admin_password" ]]; then
    echo "INITIAL_ADMIN_USERNAME and INITIAL_ADMIN_PASSWORD must be set together." >&2
    exit 1
  fi
  if [[ -n "$admin_password" ]] && { (( ${#admin_password} < 12 )) || [[ "$admin_password" == *"replace-with"* ]]; }; then
    echo "INITIAL_ADMIN_PASSWORD must contain at least 12 characters." >&2
    exit 1
  fi
  if [[ "$(configured_value INITIAL_ADMIN_FORCE_RESET)" == "true" && -z "$admin_password" ]]; then
    echo "INITIAL_ADMIN_FORCE_RESET=true requires administrator credentials." >&2
    exit 1
  fi
  integration_api_key="$(configured_value INTEGRATION_API_KEY)"
  if (( ${#integration_api_key} < 24 )) || [[ "$integration_api_key" == *"replace-with"* ]]; then
    echo "Set INTEGRATION_API_KEY to a non-placeholder secret of at least 24 characters." >&2
    exit 1
  fi
  if [[ "$(configured_value ALLOW_INSECURE_DEFAULT_ADMIN)" != "false" ]]; then
    echo "Set ALLOW_INSECURE_DEFAULT_ADMIN=false in cloud database mode." >&2
    exit 1
  fi
  embedding_dimensions="$(configured_value EMBEDDING_DIMENSIONS)"
  if [[ -n "$embedding_dimensions" && "$embedding_dimensions" != "1024" ]]; then
    echo "Cloud deployment must preserve EMBEDDING_DIMENSIONS=1024." >&2
    exit 1
  fi
fi

tei_gpu_image="${TEI_GPU_IMAGE:-}"
if [[ -z "$tei_gpu_image" ]]; then
  tei_gpu_image="$(dotenv_value TEI_GPU_IMAGE)"
fi
tei_gpu_image="${tei_gpu_image:-ghcr.io/huggingface/text-embeddings-inference:cuda-1.8.3}"

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

compose=(docker compose -p "$project_name" -f docker-compose.yml)
if [[ "$database_mode" == "local" ]]; then
  compose+=(-f docker-compose.local.yml)
fi
compose+=(-f "$override_file")

stop_initialization_containers() {
  "${compose[@]}" stop -t 10 migrate >/dev/null 2>&1 || true
}

echo "Database mode: $database_mode"
echo "Selected runtime: $selected_runtime"
"${compose[@]}" config --quiet
if [[ "$database_mode" == "cloud" ]] && "${compose[@]}" config --services | grep -qx postgres; then
  echo "Cloud deployment configuration unexpectedly contains a local PostgreSQL service." >&2
  exit 1
fi

deadline=$((SECONDS + timeout_seconds))
remaining_seconds=$((deadline - SECONDS))
up_exit_code=0
timeout --foreground "${remaining_seconds}s" \
  "${compose[@]}" up -d --build --remove-orphans || up_exit_code=$?
if (( up_exit_code != 0 )); then
  "${compose[@]}" logs --tail 150 migrate >&2 || true
  if (( up_exit_code == 124 )); then
    stop_initialization_containers
    echo "Docker Compose startup timed out after ${timeout_seconds}s." >&2
  fi
  echo "Docker Compose failed to start the selected runtime." >&2
  exit 1
fi

migrate_container="$("${compose[@]}" ps -a -q migrate)"
if [[ -n "$migrate_container" ]]; then
  migrate_exit_code="$(docker inspect --format '{{.State.ExitCode}}' "$migrate_container")"
  if [[ "$migrate_exit_code" != "0" ]]; then
    "${compose[@]}" logs --tail 150 migrate >&2 || true
    echo "Database initialization failed." >&2
    exit 1
  fi
fi

until "${compose[@]}" exec -T backend python -c \
  "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=3).read()" \
  >/dev/null 2>&1; do
  if (( SECONDS >= deadline )); then
    echo "Database, backend, or embedding readiness timed out after ${timeout_seconds}s." >&2
    "${compose[@]}" ps >&2 || true
    "${compose[@]}" logs --tail 100 migrate embedding-qwen backend >&2 || true
    stop_initialization_containers
    exit 1
  fi
  sleep 2
done

remaining_seconds=$((deadline - SECONDS))
(( remaining_seconds > 0 )) || remaining_seconds=1
embedding_exit_code=0
timeout --foreground "${remaining_seconds}s" \
  "${compose[@]}" exec -T backend python -m app.scripts.smoke_embedding \
  || embedding_exit_code=$?
if (( embedding_exit_code != 0 )); then
  "${compose[@]}" logs --tail 100 embedding-qwen backend >&2 || true
  if (( embedding_exit_code == 124 )); then
    echo "Embedding smoke test timed out." >&2
  fi
  echo "Embedding smoke test failed." >&2
  exit 1
fi

# Run the destructive media put/get/delete probe exactly once after readiness.
remaining_seconds=$((deadline - SECONDS))
(( remaining_seconds > 0 )) || remaining_seconds=1
media_exit_code=0
timeout --foreground "${remaining_seconds}s" \
  "${compose[@]}" exec -T backend python -m app.scripts.smoke_media_storage \
  || media_exit_code=$?
if (( media_exit_code != 0 )); then
  "${compose[@]}" logs --tail 100 backend >&2 || true
  if (( media_exit_code == 124 )); then
    echo "Media storage smoke test timed out." >&2
  fi
  echo "Media storage smoke test failed." >&2
  exit 1
fi

"${compose[@]}" ps
echo "Deployment completed: database=$database_mode, runtime=$selected_runtime"
