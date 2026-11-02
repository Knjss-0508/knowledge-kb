#!/bin/bash
# 检索事件保留策略执行脚本（默认保留 90 天，人工反馈记录永久保留）
# 由 root 定时任务调用：30 3 * * * /opt/knowledge-kb/scripts/retention/run-prune.sh >> /var/log/kb-retention.log 2>&1
#
# 2026-11-02 修复：原来硬编码 knowledge-kb-backend:latest。该标签在 2026-10-31
# 清理镜像时被删掉后，脚本连续 3 天静默失败（docker 去拉取远端同名镜像，
# 被镜像代理白名单拒绝），retrieval_quality_events 涨到 1.2M 行 / 2.4GB。
# 现在改为跟随正在运行的 kb-backend 容器镜像；也可以用 KB_IMAGE 显式覆盖。
set -u
cd /opt/knowledge-kb || exit 1

DATABASE_URL="$(grep -m1 '^DATABASE_URL=' .env | cut -d= -f2-)"
if [ -z "$DATABASE_URL" ]; then
  echo "$(date '+%F %T') 错误：未能从 .env 读取 DATABASE_URL，已中止"
  exit 1
fi

# 优先用显式指定的镜像，其次跟随线上正在运行的容器，最后才退回 latest。
IMAGE="${KB_IMAGE:-}"
if [ -z "$IMAGE" ]; then
  IMAGE="$(docker inspect kb-backend --format '{{.Config.Image}}' 2>/dev/null || true)"
fi
if [ -z "$IMAGE" ]; then
  IMAGE="knowledge-kb-backend:latest"
fi
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "$(date '+%F %T') 错误：镜像 $IMAGE 本地不存在，已中止（避免误拉远端同名镜像）"
  exit 1
fi

echo "$(date '+%F %T') 使用镜像 $IMAGE"

docker run --rm --network knowledge-kb_default \
  -e DATABASE_URL="$DATABASE_URL" \
  -e TZ=Asia/Shanghai \
  -e RETENTION_DAYS="${RETENTION_DAYS:-90}" \
  -v /opt/knowledge-kb/scripts/retention:/retention:ro \
  --entrypoint python \
  "$IMAGE" /retention/prune_retrieval_events.py "$@"
