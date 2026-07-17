#!/usr/bin/env bash
set -euo pipefail

PROJECT_NAME="${COMPOSE_PROJECT_NAME:-knowledge-kb}"
EMBEDDING_MODE="${EMBEDDING_MODE:-gpu}"

cd "$(dirname "$0")/.."

case "$EMBEDDING_MODE" in
  cpu|gpu) ;;
  *)
    echo "EMBEDDING_MODE must be cpu or gpu, got: $EMBEDDING_MODE" >&2
    exit 1
    ;;
esac

EMBEDDING_COMPOSE_FILE="docker-compose.embedding-${EMBEDDING_MODE}.yml"
if [ ! -f "$EMBEDDING_COMPOSE_FILE" ]; then
  echo "Embedding Compose file not found: $EMBEDDING_COMPOSE_FILE" >&2
  exit 1
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example. Review it before production use."
fi

docker compose -p "$PROJECT_NAME" -f docker-compose.yml -f "$EMBEDDING_COMPOSE_FILE" up -d --build
docker compose -p "$PROJECT_NAME" -f docker-compose.yml -f "$EMBEDDING_COMPOSE_FILE" ps
