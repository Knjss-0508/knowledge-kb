#!/bin/bash
# 检索事件保留策略执行脚本（默认保留 90 天，人工反馈记录永久保留）
# 由 root 定时任务调用：30 3 * * * /opt/knowledge-kb/scripts/retention/run-prune.sh >> /var/log/kb-retention.log 2>&1
set -u
cd /opt/knowledge-kb || exit 1

DATABASE_URL="$(grep -m1 '^DATABASE_URL=' .env | cut -d= -f2-)"
if [ -z "$DATABASE_URL" ]; then
  echo "$(date '+%F %T') 错误：未能从 .env 读取 DATABASE_URL，已中止"
  exit 1
fi

docker run --rm --network knowledge-kb_default \
  -e DATABASE_URL="$DATABASE_URL" \
  -e TZ=Asia/Shanghai \
  -e RETENTION_DAYS="${RETENTION_DAYS:-90}" \
  -v /opt/knowledge-kb/scripts/retention:/retention:ro \
  --entrypoint python \
  knowledge-kb-backend:latest /retention/prune_retrieval_events.py "$@"
