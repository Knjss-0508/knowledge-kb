#!/usr/bin/env bash
# 知识库后端容器启动脚本（按线上实际运行参数记录）
#
# 线上 kb-backend 不使用 compose 管理，而是由本脚本 `docker run` 启动，
# 因此内存/日志等运行参数必须改在这里，改 compose 不会生效。
#
# 用法: bash run-kb-backend.sh [image]
#
# 历史：
#   2026-10-09 首次按线上实际参数记录
#   2026-10-10 增加 zzdy 访问日志只读挂载（工单号判定用）
#   2026-11-02 内存治理：限制 glibc arena 并设置内存上限
set -e
IMAGE="${1:-knowledge-kb-backend:candidate-pause-10ed87b}"

# 单进程 uvicorn 有 19 个线程，glibc 默认会为每个线程建 arena，
# 每段最大 64MB 且释放后不归还系统。实测 3 小时涨到 3.5GB，
# 但 Python 侧只有 4.8k 个对象、清空业务缓存后 RSS 不变，
# 属于分配器碎片而非泄漏。限制到 2 个 arena 后碎片大幅收敛。
MALLOC_ARENA_MAX="${MALLOC_ARENA_MAX:-2}"

# 上限按治理后的预期占用（约 1.5GB）留足余量；超限时只杀本容器，
# 不再把整机拖进 IO 卡死。可用 KB_MEMORY_LIMIT 覆盖。
KB_MEMORY_LIMIT="${KB_MEMORY_LIMIT:-4g}"

docker rm -f kb-backend >/dev/null 2>&1 || true
docker run -d --name kb-backend --restart unless-stopped \
  --memory="$KB_MEMORY_LIMIT" \
  --memory-swap="$KB_MEMORY_LIMIT" \
  -e MALLOC_ARENA_MAX="$MALLOC_ARENA_MAX" \
  --log-driver json-file \
  --log-opt max-size=50m \
  --log-opt max-file=5 \
  -p 127.0.0.1:8000:8000 \
  --network knowledge-kb_default \
  --add-host=host.docker.internal:host-gateway \
  -v /opt/knowledge-kb/backend/uploads:/app/uploads \
  -v knowledge-kb_manhattan_cache:/app/data \
  -v /www/wwwlogs:/app/nlogs:ro \
  --env-file /opt/knowledge-kb/.env \
  "$IMAGE"
