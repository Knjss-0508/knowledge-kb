#!/bin/bash
# Answer Hub 切换到本机 —— 带自动回滚
# 用法: bash switch_answer_hub.sh [--dry-run]
#       --dry-run  只做前置检查与备份，不改任何配置
set -uo pipefail

DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

ENVFILE=/opt/knowledge-kb/.env
WORKDIR=/opt/knowledge-kb
NEWPORT=18780
STAMP=$(date +%Y%m%d-%H%M%S)
BAK=/opt/knowledge-kb-runtime/switch-backup-$STAMP
COMPOSE=(-f $WORKDIR/docker-compose.yml -f $WORKDIR/docker-compose.embedding-cpu.yml \
         -f /opt/knowledge-kb-runtime/docker-compose.host-pg.yml \
         -f /opt/knowledge-kb-runtime/docker-compose.server.yml \
         -f /opt/knowledge-kb-runtime/backend-host-gateway.yml \
         -f $WORKDIR/.codex-deploy-20260929-pre-release/compose.release.yml \
         -f $WORKDIR/.codex-deploy-20260929-correct-frontend/compose.frontend.json \
         -f $WORKDIR/.codex-deploy-20260930-master-f851/compose.master.yml \
         -f $WORKDIR/.codex-deploy-20260930-layout-7bc/compose.layout.yml \
         -f $WORKDIR/.codex-deploy-20260930-embedded-chat-2e1/compose.embedded-chat.yml)

log() { echo "[$(date +%H:%M:%S)] $*"; }
fatal() { log "❌ $*"; exit 1; }

KEY=$(grep -E "^ANSWER_HUB_API_KEY=" "$ENVFILE" | cut -d= -f2-)

log "==================== 阶段 1: 前置检查 ===================="
# 1.1 本机 API
c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://100.72.97.89:8780/health)
[ "$c" = "200" ] || fatal "本机 API 不可达 (HTTP $c)"; log "  ✅ 本机 API 200"

# 1.2 隧道（从服务器侧访问本机）
c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1:$NEWPORT/health)
[ "$c" = "200" ] || fatal "隧道不可达 (HTTP $c)"; log "  ✅ 隧道 127.0.0.1:$NEWPORT 200"

# 1.3 业务接口（带鉴权）
c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 -H "X-Answer-Hub-Key: $KEY" http://127.0.0.1:$NEWPORT/api/v1/automation/control)
[ "$c" = "200" ] || fatal "隧道业务接口 401/异常 (HTTP $c)"; log "  ✅ 隧道业务接口 200"

# 1.4 从 kb-backend 容器内访问（切换后的真实路径）
docker exec kb-backend python -c "
import urllib.request
r = urllib.request.urlopen('http://172.18.0.1:$NEWPORT/health', timeout=15)
assert r.status == 200, r.status
print('  ✅ kb-backend 容器内可访问 172.18.0.1:$NEWPORT')
" || fatal "kb-backend 容器内无法访问隧道端口"

# 1.5 服务器本地生产仍健康（回滚目标可用）
c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 http://127.0.0.1:8780/health)
[ "$c" = "200" ] || fatal "服务器本地 Answer Hub 不健康 (HTTP $c)，不可切换"
log "  ✅ 服务器本地 Answer Hub 200（回滚目标可用）"

log ""
log "==================== 阶段 2: 备份 ===================="
mkdir -p "$BAK"
cp "$ENVFILE" "$BAK/env.bak"
grep -nE "^ANSWER_HUB_(BASE_URL|API_BASE_URL)=" "$ENVFILE" > "$BAK/urls.before.txt"
docker inspect kb-backend > "$BAK/kb-backend.before.json" 2>/dev/null
log "  ✅ 备份至 $BAK"
cat "$BAK/urls.before.txt" | sed 's/^/      /'

if [ $DRY -eq 1 ]; then
  log ""
  log "==================== DRY-RUN 结束（未做任何修改） ===================="
  exit 0
fi

log ""
log "==================== 阶段 3: 改配置 ===================="
python3 - "$ENVFILE" "$NEWPORT" <<'PY'
import sys
path, port = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as f:
    lines = f.readlines()
out, n = [], 0
for ln in lines:
    s = ln.rstrip("\n")
    if s.startswith("ANSWER_HUB_BASE_URL="):
        out.append("ANSWER_HUB_BASE_URL=http://host.docker.internal:%s\n" % port); n += 1
    elif s.startswith("ANSWER_HUB_API_BASE_URL="):
        out.append("ANSWER_HUB_API_BASE_URL=http://172.18.0.1:%s\n" % port); n += 1
    else:
        out.append(ln)
with open(path, "w", encoding="utf-8") as f:
    f.writelines(out)
print("  已修改 %d 行" % n)
PY
grep -nE "^ANSWER_HUB_(BASE_URL|API_BASE_URL)=" "$ENVFILE" | sed 's/^/      /'

log ""
log "==================== 阶段 4: 重建 kb-backend ===================="
cd "$WORKDIR" || fatal "无法进入 $WORKDIR"
docker compose "${COMPOSE[@]}" --env-file "$ENVFILE" up -d --force-recreate --no-deps backend 2>&1 | tail -8 | sed 's/^/      /'

log "  等待容器健康..."
ok=0
for i in $(seq 1 30); do
  sleep 10
  h=$(docker inspect kb-backend --format '{{.State.Health.Status}}' 2>/dev/null)
  s=$(docker inspect kb-backend --format '{{.State.Status}}' 2>/dev/null)
  log "      +$((i*10))s  状态=$s  健康=$h"
  if [ "$h" = "healthy" ]; then ok=1; break; fi
done

rollback() {
  log ""
  log "==================== 回滚 ===================="
  cp "$BAK/env.bak" "$ENVFILE"
  log "  已还原 .env"
  docker compose "${COMPOSE[@]}" --env-file "$ENVFILE" up -d --force-recreate --no-deps backend 2>&1 | tail -5 | sed 's/^/      /'
  for i in $(seq 1 30); do
    sleep 10
    h=$(docker inspect kb-backend --format '{{.State.Health.Status}}' 2>/dev/null)
    log "      +$((i*10))s 健康=$h"
    [ "$h" = "healthy" ] && { log "  ✅ 回滚完成，容器已恢复健康"; break; }
  done
  log "  回滚备份保留在: $BAK"
}

[ $ok -eq 1 ] || { log "  ❌ 容器未在 300 秒内健康"; rollback; exit 1; }
log "  ✅ kb-backend 已健康"

log ""
log "==================== 阶段 5: 验证 ===================="
E=$(docker inspect kb-backend --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -E "^ANSWER_HUB_(BASE_URL|API_BASE_URL)=")
log "  容器实际生效值:"; echo "$E" | sed 's/^/      /'
echo "$E" | grep -q ":$NEWPORT" || { log "  ❌ 容器内 URL 未生效"; rollback; exit 1; }

c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 https://knowledgekb.powerzhuan.cn/ 2>/dev/null)
log "  公网站点: HTTP $c"
c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 http://127.0.0.1:8780/health)
log "  服务器本地 Answer Hub（保留未停）: HTTP $c"

log ""
log "==================== 阶段 6: 观察本机是否收到请求 ===================="
sleep 20
tail -8 /var/log/nginx/access.log 2>/dev/null | grep -c . >/dev/null
log "  请在本机执行: Get-Content E:\\answer-hub-runtime\\api-access.log -Tail 20"
log "  确认能看到来自 kb-backend 的请求"

log ""
log "==================== 切换完成 ✅ ===================="
log "  备份: $BAK"
log "  回滚命令: cp $BAK/env.bak $ENVFILE && 重建 backend"
