#!/usr/bin/env bash
# 清理 knowledge-kb-backend 的历史镜像，只保留明确需要的。
#
# 背景（2026-11-02）：一次部署 build 一个新 tag，旧镜像从不清理，
# 累积到 53 个 / 9.6 GB。历史上靠手工 `docker rmi`，既慢又容易误删。
#
# 保留规则（四条并集）：
#   1. 当前正在运行的镜像；
#   2. 所有容器（含已停止）实际引用的镜像；
#   3. 最近 3 个构建的镜像（便于快速回退上一次发布）；
#   4. 文档里记录过的回滚锚点（docs/*.md 中的 run-kb-backend.sh 参数）。
#
# 用法：
#   ./prune-kb-images.sh             # 只打印清单，不删除
#   ./prune-kb-images.sh --apply     # 真正删除
#
# 注意：不要用 `docker image prune -a`。本机 6+ 项目共用 Docker，
# 全局 prune 会删掉其他项目的镜像。

set -u

REPO="${KB_IMAGE_REPO:-knowledge-kb-backend}"
MODE="${1:---dry-run}"
KEEP_RECENT="${KB_KEEP_RECENT:-3}"

keep=" "
add_keep() {
    local tag="$1"
    [ -z "$tag" ] && return 0
    case "$keep" in
        *" $tag "*) ;;
        *) keep="$keep$tag " ;;
    esac
}

# 1) 当前运行镜像
running=$(docker inspect kb-backend --format '{{.Config.Image}}' 2>/dev/null || true)
add_keep "${running#*:}"

# 2) 所有容器引用的镜像
while read -r img; do
    case "$img" in
        "$REPO":*) add_keep "${img#*:}" ;;
    esac
done < <(docker ps -a --format '{{.Image}}')

# 3) 最近 N 个
for tag in $(docker images "$REPO" --format '{{.CreatedAt}}|{{.Tag}}' \
             | sort -r | head -n "$KEEP_RECENT" | cut -d'|' -f2); do
    add_keep "$tag"
done

# 4) 显式回滚锚点清单
#
# 不扫描 docs/：服务器上的 /opt/knowledge-kb 是 2026-09-17 的旧检出，
# 不含近期上线文档；靠扫文档会漏掉真正要保留的锚点。
# 这里维护一份显式清单，新增上线记录时同步追加。
ANCHORS="${KB_ROLLBACK_ANCHORS:-}"
if [ -z "$ANCHORS" ] && [ -f /opt/knowledge-kb-runtime/kb-rollback-anchors.txt ]; then
    # 去掉注释行与空行，只保留 tag
    ANCHORS=$(grep -vE '^\s*(#|$)' /opt/knowledge-kb-runtime/kb-rollback-anchors.txt)
fi
if [ -z "$ANCHORS" ]; then
    echo "警告：未找到回滚锚点清单（KB_ROLLBACK_ANCHORS 或 /opt/knowledge-kb-runtime/kb-rollback-anchors.txt）" >&2
    echo "      为避免误删回滚镜像，脚本中止。先用 KB_ROLLBACK_ANCHORS='t1 t2' 显式指定。" >&2
    exit 1
fi
for tag in $ANCHORS; do
    add_keep "$tag"
done

# 5) compose 钉住的镜像（删掉会导致 compose 启动失败）
for f in /opt/knowledge-kb-runtime/compose.pin-image.yml; do
    [ -f "$f" ] || continue
    for tag in $(grep -oE "${REPO}:[A-Za-z0-9._-]+" "$f" | sed "s/.*${REPO}://" | sort -u); do
        add_keep "$tag"
    done
done

retained=$(echo "$keep" | tr ' ' '\n' | grep -v '^$' | sort)
echo "=== 保留 $(echo "$retained" | wc -l) 个镜像 ==="
echo "$retained" | sed 's/^/  保留 /'

total=0
deleted=0
skipped=0
for tag in $(docker images "$REPO" --format '{{.Tag}}'); do
    total=$((total + 1))
    case "$keep" in
        *" $tag "*) continue ;;
    esac
    if [ "$MODE" = "--apply" ]; then
        if docker rmi "$REPO:$tag" >/dev/null 2>&1; then
            echo "已删除 $REPO:$tag"
            deleted=$((deleted + 1))
        else
            echo "跳过   $REPO:$tag（仍被引用）"
            skipped=$((skipped + 1))
        fi
    else
        echo "待删除 $REPO:$tag"
        deleted=$((deleted + 1))
    fi
done

if [ "$MODE" = "--apply" ]; then
    echo "=== 共 $total 个，已删除 $deleted 个，跳过 $skipped 个 ==="
else
    echo "=== 共 $total 个，待删除 $deleted 个（未执行，用 --apply 删除）==="
fi
[ "$MODE" = "--apply" ] && docker system df
