# Docker Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the project deployable on a typical Ubuntu cloud server with one Docker Compose command.

**Architecture:** FastAPI serves the API and static frontend in one backend container. PostgreSQL, Redis, and Elasticsearch run as managed Compose services with persistent Docker volumes. Environment-specific values live in `.env`.

**Tech Stack:** FastAPI, Uvicorn, PostgreSQL, Redis, Elasticsearch, Docker Compose.

---

### Task 1: Portable Backend Paths

**Files:**
- Modify: `backend/app/main.py`

- [x] Replace the Windows absolute project path with `Path(__file__).resolve()`.
- [x] Convert static mounts and file responses to use portable paths.

### Task 2: Compose Deployment

**Files:**
- Modify: `docker-compose.yml`
- Modify: `backend/Dockerfile`
- Create: `.env.example`
- Create: `.dockerignore`

- [x] Parameterize ports and database credentials.
- [x] Keep backend, PostgreSQL, Redis, and Elasticsearch in one Compose stack.
- [x] Copy both backend and frontend into the backend image.
- [x] Exclude local virtualenv, uploads, and logs from backend image builds.

### Task 3: Deployment Scripts And Docs

**Files:**
- Create: `scripts/deploy.sh`
- Create: `scripts/deploy.ps1`
- Create: `docs/deploy.md`

- [x] Add one-command deployment scripts.
- [x] Document first deployment, local update flow, server sync flow, logs, stop, restart, and backup.

### Task 4: Verification

**Files:**
- No new files.

- [ ] Run `docker compose -p knowledge-kb config`.
- [ ] Run a backend import check where dependencies are available.
- [ ] Report any verification gaps caused by local Docker Hub/network limits.
