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

# ⚠️ 关键安全措施：把 backend 的镜像钉死在「当前正在运行的镜像」上
#
# 背景：/opt/knowledge-kb/docker-compose.yml 里 backend 服务用的是 build: 而非 image:，
#       因此 `docker compose up` 会【重新构建镜像】，把生产代码换成当前 checkout 的版本，
#       而不是继续使用正在运行的镜像。切换只是改两个环境变量，绝不能顺带发布代码 ——
#       本仓库今天已因此出过事故（alembic 迁移版本缺失导致 kb-backend 起不来）。
#
# 做法：运行时读取 kb-backend 当前镜像，生成 override 文件钉死，并配合 --no-build。
PINFILE=/opt/knowledge-kb-runtime/compose.pin-image.yml
CURRENT_IMAGE=$(docker inspect kb-backend --format '{{.Config.Image}}' 2>/dev/null)
if [ -z "$CURRENT_IMAGE" ]; then
    echo "❌ 无法读取 kb-backend 当前镜像，拒绝继续（避免误重建）" >&2
    exit 1
fi
case "$CURRENT_IMAGE" in
    *:*) ;;
    *) echo "❌ 镜像名缺少 tag（$CURRENT_IMAGE），拒绝继续" >&2; exit 1 ;;
esac
cat > "$PINFILE" <<PINEOF
# 由 switch_answer_hub.sh 自动生成：钉死镜像，避免切换时误重建代码
services:
  backend:
    image: $CURRENT_IMAGE
PINEOF
COMPOSE+=(-f "$PINFILE")
echo "[switch] 已钉死镜像: $CURRENT_IMAGE"
echo "[switch] override:   $PINFILE"

log() { echo "[$(date +%H:%M:%S)] $*"; }
fatal() { log "❌ $*"; exit 1; }

KEY=$(grep -E "^ANSWER_HUB_API_KEY=" "$ENVFILE" | cut -d= -f2-)

# 记录切换前的两行 URL 值，用于结尾打印回滚信息（备份文件 urls.before.txt 才是权威副本）
URL_BEFORE=$(grep -E "^ANSWER_HUB_(BASE_URL|API_BASE_URL)=" "$ENVFILE")

# 只读探测：输出 HTTP 状态码，失败时输出 000。
# set -uo pipefail 下 curl 非零退出不会终止脚本，这里显式兜底。
http_code() {
  local url="$1" timeout="${2:-10}" extra="${3:-}"
  local c
  c=$(curl -s -o /dev/null -w '%{http_code}' --max-time "$timeout" $extra "$url" 2>/dev/null)
  [ -n "$c" ] || c=000
  printf '%s' "$c"
}

# ⚠️ kb-backend 没有 /health 端点（实测 HTTP 000，连接层面无此路由）。
#    判断它是否真的可对外服务，唯一可靠的端点是 /ready：
#      - 依赖就绪 → HTTP 200 {"status":"ready"}
#      - 依赖未就绪 → HTTP 503 {"status":"not_ready","errors":[...]}
#    容器自身的 healthcheck 用的也是 /ready。
KB_BACKEND_URL=http://127.0.0.1:8000
PUBLIC_URL=https://knowledgekb.powerzhuan.cn

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
docker compose "${COMPOSE[@]}" --env-file "$ENVFILE" up -d --force-recreate --no-deps --no-build backend 2>&1 | tail -8 | sed 's/^/      /'

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
  if [ $DRY -eq 1 ]; then
    log "  [dry-run] 跳过回滚（未改任何生产配置）"
    return 0
  fi
  log ""
  log "==================== 回滚 ===================="
  cp "$BAK/env.bak" "$ENVFILE"
  log "  已还原 .env"
  docker compose "${COMPOSE[@]}" --env-file "$ENVFILE" up -d --force-recreate --no-deps --no-build backend 2>&1 | tail -5 | sed 's/^/      /'
  for i in $(seq 1 30); do
    sleep 10
    h=$(docker inspect kb-backend --format '{{.State.Health.Status}}' 2>/dev/null)
    log "      +$((i*10))s 健康=$h"
    [ "$h" = "healthy" ] && { log "  ✅ 回滚完成，容器已恢复健康"; break; }
  done
  log "  回滚备份保留在: $BAK"
}

# 镜像漂移自检：线上可能手动重建过，先报告（不改容器），供人工判断
NOW_IMAGE=$(docker inspect kb-backend --format '{{.Config.Image}}' 2>/dev/null)
IMG_WAS=$(docker inspect kb-backend --format '{{.Image}}' 2>/dev/null)
if [ -n "$NOW_IMAGE" ] && [ "$NOW_IMAGE" != "$CURRENT_IMAGE" ]; then
  log "  ⚠️ 注意：重启前镜像已与脚本开头钉死值不同（可能有人手动重建过）"
  log "     脚本开头: $CURRENT_IMAGE"
  log "     当前容器: $NOW_IMAGE"
fi

[ $ok -eq 1 ] || { log "  ❌ 容器未在 300 秒内健康"; rollback; exit 1; }
log "  ✅ kb-backend 已健康"

# ---- 改进 2：区分「容器已重建」与「镜像未变」 ----
# 重建必须确实发生过（否则改的 .env 不会进容器），但镜像必须与切换前完全一致，
# 否则说明切换顺带发布了代码 —— 这是最后一道防线，直接回滚。
NEW_IMAGE=$(docker inspect kb-backend --format '{{.Config.Image}}' 2>/dev/null)
NEW_IMAGE_ID=$(docker inspect kb-backend --format '{{.Image}}' 2>/dev/null)
log "  容器已重建：镜像 $NEW_IMAGE / imageID $NEW_IMAGE_ID"
if [ "$NEW_IMAGE" != "$CURRENT_IMAGE" ]; then
  log "  ❌ 镜像漂移：当前镜像与切换前钉死的镜像不一致（切换顺带发布了代码？）"
  log "     切换前: $CURRENT_IMAGE"
  log "     当前值: $NEW_IMAGE"
  rollback; exit 1
fi
if [ -n "$IMG_WAS" ] && [ "$NEW_IMAGE_ID" != "$IMG_WAS" ]; then
  log "  ❌ 异常：容器已重建，但镜像 ID 与重建前不同（同 tag 指向了新构建的镜像？）"
  log "     重建前 imageID: $IMG_WAS"
  log "     当前   imageID: $NEW_IMAGE_ID"
  rollback; exit 1
fi
log "  ✅ 镜像未变（仍为切换前钉死的 $CURRENT_IMAGE），配置切换未夹带代码发布"

log ""
log "==================== 阶段 5: 验证 ===================="
E=$(docker inspect kb-backend --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -E "^ANSWER_HUB_(BASE_URL|API_BASE_URL)=")
log "  容器实际生效值:"; echo "$E" | sed 's/^/      /'
echo "$E" | grep -q ":$NEWPORT" || { log "  ❌ 容器内 URL 未生效"; rollback; exit 1; }

# ---- 改进 1：kb-backend 可服务性必须看 /ready，不能看 /health ----
# kb-backend 没有 /health 路由（实测 HTTP 000），/ready 才是它自己的就绪探针。
log "  等待 kb-backend /ready 返回 200（最多 120s）..."
ready_ok=0
for i in $(seq 1 24); do
  rc=$(http_code "$KB_BACKEND_URL/ready" 8)
  log "      +$((i*5))s  GET $KB_BACKEND_URL/ready -> HTTP $rc"
  if [ "$rc" = "200" ]; then ready_ok=1; break; fi
  sleep 5
done
if [ $ready_ok -ne 1 ]; then
  body=$(curl -s --max-time 8 "$KB_BACKEND_URL/ready" 2>/dev/null | head -c 300)
  [ -n "$body" ] || body="(无响应体)"
  log "  ❌ kb-backend /ready 未在 120 秒内返回 200（最后状态 HTTP $rc）"
  log "     响应: $body"
  log "     注意：kb-backend 没有 /health 端点，HTTP 000 属预期，不代表可服务性"
  rollback; exit 1
fi
rbody=$(curl -s --max-time 8 "$KB_BACKEND_URL/ready" 2>/dev/null | head -c 200)
log "  ✅ kb-backend $KB_BACKEND_URL/ready 200  响应: $rbody"

# 公网入口（宝塔 nginx 反代 127.0.0.1:8000），保留检查并轮询等待
pub_ok=0
for i in $(seq 1 6); do
  c=$(http_code "$PUBLIC_URL/ready" 15)
  log "  公网站点 $PUBLIC_URL/ready: HTTP $c"
  case "$c" in 2*) pub_ok=1; break ;; esac
  sleep 5
done
c=$(http_code "$PUBLIC_URL/" 15)
log "  公网站点首页: HTTP $c"
if [ $pub_ok -eq 1 ]; then
  log "  ✅ 公网入口可用"
else
  log "  ⚠️ 公网 /ready 未返回 2xx（$(http_code "$PUBLIC_URL/ready" 15)）；容器本地已 200，"
  log "     可能是宝塔 nginx 或 CDN 侧问题，请人工确认后再处置"
fi

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

# ---- 改进 3：打印回滚所需信息，供人工处置（不打印任何密钥值） ----
URL_AFTER=$(grep -E "^ANSWER_HUB_(BASE_URL|API_BASE_URL)=" "$ENVFILE")
COMPOSE_CMD=""
for f in "${COMPOSE[@]}"; do
  COMPOSE_CMD="$COMPOSE_CMD -f $f"
done

log ""
log "---- 回滚所需信息（请留存） ----"
log "  [.env 切换前]"
printf '%s\n' "$URL_BEFORE" | sed 's/^/      /'
log "  [.env 切换后]"
printf '%s\n' "$URL_AFTER" | sed 's/^/      /'
log "  备份目录:            $BAK"
log "  备份的 .env:         $BAK/env.bak"
log "  切换前 URL 快照:     $BAK/urls.before.txt"
log "  切换前容器配置:      $BAK/kb-backend.before.json"
log "  镜像（未变）:        $CURRENT_IMAGE"
log "  回滚方法（把 .env 改回 :8780 后重建；--no-build + 镜像钉死文件保证不重新构建代码）:"
log "      cp $BAK/env.bak $ENVFILE"
log "      cd $WORKDIR && docker compose$COMPOSE_CMD \\"
log "        --env-file $ENVFILE up -d --force-recreate --no-deps --no-build backend"
log "  说明：以上命令中的 $PINFILE 已钉死镜像 $CURRENT_IMAGE"
log "  回滚后核验:  curl -s -o /dev/null -w '%{http_code}\\n' $KB_BACKEND_URL/ready   # 期望 200"
