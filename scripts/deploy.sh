#!/usr/bin/env bash
set -euo pipefail

PROJECT_NAME="${COMPOSE_PROJECT_NAME:-knowledge-kb}"

cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example. Review it before production use."
fi

docker compose -p "$PROJECT_NAME" up -d --build
docker compose -p "$PROJECT_NAME" ps
