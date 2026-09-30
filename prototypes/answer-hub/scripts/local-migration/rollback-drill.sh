#!/bin/bash
# ============================================================
# 回滚演练（用户已授权，方案 A）
# 目标：验证「回退到旧服务」这条路真能走通，然后恢复当前状态
# 终态必须回到：.env 指向 :18780、旧服务 inactive、:8780 无监听
# ============================================================
# ⚠️ 本文件是运行时脚本 E:\答疑中台迁移\_drill_rollback.sh 的仓库镜像（已脱敏）：
#     服务器公网地址 → <SERVER_HOST>；本机 Tailscale IP → <LOCAL_TAILSCALE_IP>；公网域名 → <PUBLIC_DOMAIN>
#     本仓库为 public，禁止把真实地址、Token、密码写回本文件。
#     ⭐ 本脚本对 .env 的修改一律「按行号整行覆盖」，因此脚本本身不需要、也绝不允许
#        携带任何真实取值（密钥/Token/密码）。
#     实际运行时请在本机用原文件；本镜像仅用于留档与评审。
#
# 实测结论（2026-09-30 16:07 执行通过）：回退方向 /ready 6 秒恢复，前滚方向 4 秒恢复，
# 两次重建镜像名与 ID 均未变。详见 docs/answer-hub-迁移执行记录.md 第三节。
#
# 使用方法：把本脚本上传到服务器后
#   ssh root@<SERVER_HOST> 'bash -s' < rollback-drill.sh
set -u
WORKDIR=/opt/knowledge-kb
ENVFILE=/opt/knowledge-kb/.env
PINFILE=/opt/knowledge-kb-runtime/compose.pin-image.yml
BK=/opt/knowledge-kb-runtime/drill-backup-$(date +%Y%m%d-%H%M%S)
mkdir -p "$BK"

COMPOSE=(-f $WORKDIR/docker-compose.yml -f $WORKDIR/docker-compose.embedding-cpu.yml
         -f /opt/knowledge-kb-runtime/docker-compose.host-pg.yml
         -f /opt/knowledge-kb-runtime/docker-compose.server.yml
         -f /opt/knowledge-kb-runtime/backend-host-gateway.yml
         -f $WORKDIR/.codex-deploy-20260929-pre-release/compose.release.yml
         -f $WORKDIR/.codex-deploy-20260929-correct-frontend/compose.frontend.json
         -f $WORKDIR/.codex-deploy-20260930-master-f851/compose.master.yml
         -f $WORKDIR/.codex-deploy-20260930-layout-7bc/compose.layout.yml
         -f $WORKDIR/.codex-deploy-20260930-embedded-chat-2e1/compose.embedded-chat.yml
         -f "$PINFILE")

recreate() {   # $1 = 说明
  echo "      >>> 重建 kb-backend（$1）"
  cd $WORKDIR
  # --no-build + 钉镜像文件：只换配置，绝不夹带代码重新构建
  docker compose "${COMPOSE[@]}" --env-file "$ENVFILE" up -d --force-recreate --no-deps --no-build backend 2>&1 | tail -4 | sed 's/^/        /'
}

wait_ready() { # 最多等 60 秒
  for i in $(seq 1 30); do
    C=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8000/ready)
    if [ "$C" = "200" ]; then echo "      /ready=200（第 $((i*2)) 秒）"; return 0; fi
    sleep 2
  done
  echo "      ❌ /ready 60 秒内未恢复（最后=$C）"; return 1
}

setenv() {     # $1 = 端口（按行号整行覆盖，不回显取值）
  sed -i "88s|.*|ANSWER_HUB_BASE_URL=http://host.docker.internal:$1|" "$ENVFILE"
  sed -i "93s|.*|ANSWER_HUB_API_BASE_URL=http://172.18.0.1:$1|" "$ENVFILE"
  echo "      .env 88/93 现为:"; sed -n '88p;93p' "$ENVFILE" | sed 's/^/        /'
}

echo "############ 阶段 0：留档 ############"
cp "$ENVFILE" "$BK/env.before-drill.bak"
docker inspect kb-backend > "$BK/kb-backend.before-drill.json"
echo "  备份目录: $BK"
echo "  演练前镜像: $(docker inspect kb-backend --format '{{.Config.Image}}')"
echo ""

echo "############ 阶段 1：启动旧服务（临时）############"
systemctl start answer-hub-api
sleep 4
echo "  is-active  = $(systemctl is-active answer-hub-api)"
echo "  is-enabled = $(systemctl is-enabled answer-hub-api)"
echo "  :8780/health = $(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8780/health)"
echo ""

echo "############ 阶段 2：改 .env 回退到 :8780（演练回滚）############"
setenv 8780
echo ""
echo "############ 阶段 3：重建容器（用旧路径）############"
recreate "回退方向"
echo "  等待 /ready ..."
if ! wait_ready; then
  echo "  ⚠️⚠️ 回退方向失败，立即恢复前滚方向 ⚠️⚠️"
  setenv 18780
  recreate "自动恢复"
  wait_ready
  echo "  已恢复。当前 .env:"; sed -n '88p;93p' "$ENVFILE" | sed 's/^/    /'
  exit 1
fi
echo ""
echo "############ 阶段 4：验证回退方向确实生效 ############"
echo "  容器内指向: $(docker exec kb-backend printenv ANSWER_HUB_API_BASE_URL 2>&1)"
echo "  镜像是否未变: $(docker inspect kb-backend --format '{{.Config.Image}}')"
echo "  容器内 e2e（现在应打到服务器本地旧服务）:"
docker exec kb-backend python /tmp/e2e.py 2>&1 | tail -3 | sed 's/^/    /'
echo "  公网: $(curl -s -o /dev/null -w '%{http_code}' --max-time 12 https://<PUBLIC_DOMAIN>/ready)"
echo ""
echo "############ 阶段 5：恢复前滚方向 :18780 ############"
setenv 18780
recreate "恢复前滚方向"
echo "  等待 /ready ..."
if ! wait_ready; then echo "  ❌❌ 恢复前滚失败，需要人工介入！"; exit 2; fi
echo ""
echo "############ 阶段 6：验证已回到本机生产 ############"
echo "  容器内指向: $(docker exec kb-backend printenv ANSWER_HUB_API_BASE_URL 2>&1)"
echo "  镜像: $(docker inspect kb-backend --format '{{.Config.Image}}')"
echo "  容器内 e2e:"
docker exec kb-backend python /tmp/e2e.py 2>&1 | tail -3 | sed 's/^/    /'
echo "  公网 ready=$(curl -s -o /dev/null -w '%{http_code}' --max-time 12 https://<PUBLIC_DOMAIN>/ready)"
echo "  隧道 18780=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 http://127.0.0.1:18780/health)"
echo ""
echo "############ 阶段 7：把旧服务停回去（恢复演练前状态）############"
systemctl stop answer-hub-api
sleep 2
echo "  is-active  = $(systemctl is-active answer-hub-api)"
echo "  is-enabled = $(systemctl is-enabled answer-hub-api)"
echo "  :8780/health = $(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8780/health)"
echo ""
echo "############ 阶段 8：最终状态确认 ############"
echo "  .env 88/93:"; sed -n '88p;93p' "$ENVFILE" | sed 's/^/    /'
echo "  六个生产容器:"
docker ps --format '    {{.Names}}|{{.Status}}' | sort
echo ""
echo "  结束时间: $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "############ 演练结束 ############"
