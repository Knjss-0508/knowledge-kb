#!/bin/bash
# 嵌入隧道健康检查（在应用服务器上运行）
#
# 用途：GPU 节点掉线时，后端无法获取查询词嵌入，知识导入与依赖嵌入的召回
# 会失败，但网站其余部分看起来仍然正常，故障容易被长期忽略。
# 本脚本定时探测隧道端口，连续失败达到阈值后告警。
#
# 建议 cron（每 2 分钟）：
#   */2 * * * * /opt/knowledge-kb/scripts/check-embedding-tunnel.sh >/dev/null 2>&1
#
# 可选告警渠道：设置环境变量 EMBEDDING_TUNNEL_ALERT_WEBHOOK 为
# 飞书/企业微信机器人地址，脚本会 POST 一条文本消息。未设置时只写日志。

set -u

PORT="${EMBEDDING_TUNNEL_REMOTE_PORT:-18080}"
STATE_DIR="${EMBEDDING_TUNNEL_STATE_DIR:-/var/lib/kb-embedding-tunnel}"
STATE_FILE="$STATE_DIR/failures"
LOG_FILE="${EMBEDDING_TUNNEL_LOG:-/var/log/kb-embedding-tunnel.log}"
FAIL_THRESHOLD="${EMBEDDING_TUNNEL_FAIL_THRESHOLD:-3}"
WEBHOOK="${EMBEDDING_TUNNEL_ALERT_WEBHOOK:-}"

mkdir -p "$STATE_DIR" 2>/dev/null || true

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG_FILE" 2>/dev/null || true
}

notify() {
    local text="$1"
    log "ALERT $text"
    [ -z "$WEBHOOK" ] && return 0
    curl -s --max-time 10 -X POST "$WEBHOOK" \
        -H 'Content-Type: application/json' \
        -d "{\"msgtype\":\"text\",\"text\":{\"content\":\"$text\"}}" >/dev/null 2>&1 || true
}

# 探测隧道：TEI 的 /health 返回 200 + 空 body，因此只看退出码
if curl -s --fail --max-time 10 -o /dev/null "http://127.0.0.1:${PORT}/health"; then
    previous=$(cat "$STATE_FILE" 2>/dev/null || echo 0)
    echo 0 > "$STATE_FILE"
    if [ "$previous" -ge "$FAIL_THRESHOLD" ]; then
        notify "答疑中台：GPU 嵌入隧道已恢复（端口 ${PORT}）。"
    fi
    exit 0
fi

failures=$(cat "$STATE_FILE" 2>/dev/null || echo 0)
failures=$((failures + 1))
echo "$failures" > "$STATE_FILE"

# 仅在「首次达到阈值」时告警，避免每 2 分钟重复刷屏
if [ "$failures" -eq "$FAIL_THRESHOLD" ]; then
    notify "答疑中台告警：GPU 嵌入隧道连续 ${failures} 次探测失败（端口 ${PORT}）。知识导入与依赖查询词嵌入的召回将不可用。请检查 GPU 节点是否开机、是否有人登录、Docker Desktop 与隧道容器是否运行。"
fi

exit 0
