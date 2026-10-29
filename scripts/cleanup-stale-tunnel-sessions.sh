# 诊断（可选清理）占用指定转发端口的 sshd 会话。
#
# 背景（2026-10-29）：远端 sshd 的 ClientAliveInterval 为 0，客户端异常掉线时
# 服务端不会主动回收会话。这些“僵尸会话”仍然占着 0.0.0.0:18080 / 18780 / 19000，
# 新隧道绑定时报 `Address already in use`，只能靠容器反复重启碰运气，最终把
# 后端拖成 `Embedding unavailable`。
#
# 用法（在应用服务器上，需要 root）：
#   ./cleanup-stale-tunnel-sessions.sh                # 默认只诊断，不杀进程
#   ./cleanup-stale-tunnel-sessions.sh --force        # 清理“端口已释放但会话仍在”的
#   ./cleanup-stale-tunnel-sessions.sh --port 18080   # 只处理一个端口
#
# 为什么默认不清理：反向转发端口在空闲时本来就没有 ESTABLISHED 连接，
# 用“该端口有无连接”判断会话死活会误杀健康隧道（2026-10-29 实测会把
# 正常的 18780/19000 判成残留）。因此默认只输出诊断信息，由人确认后再
# 用 --force；根治手段是服务端的 ClientAliveInterval（见 docs）。
#
# --force 的判定条件（必须同时满足）：
#   1. 该 pid 是 sshd 子进程且不是 :22 主监听进程；
#   2. 该 pid 已经不在任何转发端口的监听列表里（即已丢失端口，纯残留）。

set -u

PORTS="18080 18780 19000"
FORCE=0

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) FORCE=0; shift ;;
        --force) FORCE=1; shift ;;
        --port) PORTS="$2"; shift 2 ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "未知参数：$1" >&2; exit 2 ;;
    esac
done

if [ "$(id -u)" -ne 0 ]; then
    echo "需要 root 权限（读取 ss 的进程信息并结束 sshd 子进程）。" >&2
    exit 1
fi

cleanup_port() {
    local port="$1"
    local listeners pids pid

    listeners=$(ss -lntp 2>/dev/null | grep -E ":${port}[[:space:]]" || true)
    if [ -z "$listeners" ]; then
        echo "[$port] 未监听，跳过"
        return 0
    fi

    pids=$(echo "$listeners" | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u)
    for pid in $pids; do
        # 主监听进程（SSHD_MAIN_PID）只负责 accept，不能 kill。
        if [ "$pid" = "$SSHD_MAIN_PID" ]; then
            echo "[$port] pid=$pid 是 sshd 主进程，跳过"
            continue
        fi

        local cmd
        cmd=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)
        case "$cmd" in
            *sshd*) ;;
            *) echo "[$port] pid=$pid 不是 sshd（$cmd），跳过"; continue ;;
        esac

        # 有活跃对端说明会话仍在服务，保留。
        # 注意必须用「本地端口」判断：转发连接在 ss 里显示为
        #   172.17.0.1:18080  172.18.0.3:<临时端口>  users:(("sshd",pid=...))
        # 直接 grep ":18080" 会同时命中监听行和连接行，容易把有流量的
        # 会话误判成残留（2026-10-29 的 dry-run 就误报了 18780/19000）。
        local established
        established=$(ss -Htn state established "( sport = :${port} )" 2>/dev/null | wc -l)
        if [ "${established:-0}" -gt 0 ]; then
            echo "[$port] pid=$pid 仍有 $established 条活跃连接，保留"
            continue
        fi

        # 空闲的转发会话本来就没有 ESTABLISHED 连接，不能据此判定死活。
        # 默认只报告，交由人工确认（根治靠服务端 ClientAliveInterval）。
        echo "[$port] pid=$pid 正在监听（无活跃连接属正常空闲状态）"
    done
}

# 主监听进程：占用 :22 的那个 sshd，避免误杀。
SSHD_MAIN_PID=$(ss -lntp 2>/dev/null | grep -E ":22[[:space:]]" | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2)

for port in $PORTS; do
    cleanup_port "$port"
done

if [ "$FORCE" -eq 1 ]; then
    echo "提示：--force 只清理「已不监听任何转发端口」的 sshd 子进程。"
    for pid in $(pgrep -f "^sshd: root" 2>/dev/null); do
        [ "$pid" = "$SSHD_MAIN_PID" ] && continue
        if ! ss -lntp 2>/dev/null | grep -q "pid=$pid,"; then
            if kill -TERM "$pid" 2>/dev/null; then
                echo "已清理失去端口的残留会话 pid=$pid"
            fi
        fi
    done
fi
