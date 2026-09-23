#!/bin/bash
# 在宝塔服务器上部署 Adminer（PostgreSQL 网页查询界面），只对 Tailscale 内网开放。
#
# 背景：宝塔的 PostgreSQL 管理器插件只做运维（启停/改配置/备份恢复/改密码/扩展管理），
# 没有表浏览和 SQL 窗口；phpMyAdmin 是 MySQL 专用。本脚本补上「浏览器里查 PG 表」的能力。
#
# 用法：
#   sudo bash scripts/deploy-adminer.sh                      # 部署（自动生成 Basic 密码）
#   sudo BASIC_PASSWORD='xxx' bash scripts/deploy-adminer.sh # 指定密码
#   sudo bash scripts/deploy-adminer.sh --verify             # 只做安全检查
#   sudo bash scripts/deploy-adminer.sh --uninstall          # 卸载站点
#
# 环境变量：
#   TAILNET_IP     nginx 监听地址，默认自动从 `tailscale ip -4` 读取
#   PORT           监听端口，默认 8090
#   ADMINER_VERSION 默认 4.8.1（见下方说明，不要随便升 6.x）
#   BASIC_USER     默认 admin
#   BASIC_PASSWORD 默认随机生成，写入 /root/.adminer-tailnet-password
#   PHP_VER        默认 74
#
# ⚠️ Adminer 版本：6.1.0 在 PHP 7.4 下【登录必失败】——POST 返回 302 看着成功，
#    跟跳转返回 403，然后静默退回登录页且不报任何错。语法检查是通过的，
#    所以语法检查不能替代运行时验证。4.8.1 明确支持 PHP 5.3~7.4，一次通过。
set -u

PORT="${PORT:-8090}"
ADMINER_VERSION="${ADMINER_VERSION:-4.8.1}"
BASIC_USER="${BASIC_USER:-admin}"
PHP_VER="${PHP_VER:-74}"
MODE="${1:-install}"

DOCROOT="/www/wwwroot/adminer-tailnet"
CONF="/www/server/panel/vhost/nginx/adminer-tailnet.conf"
# 凭据【不能】放在 /www/server/panel/vhost/ 下 —— 见下方说明
AUTHDIR="/etc/adminer-tailnet"
AUTHFILE="$AUTHDIR/htpasswd"
PWFILE="/root/.adminer-tailnet-password"

log()  { printf '  %s\n' "$*"; }
fail() { printf '  [错误] %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "请用 root 运行"

# ---- 自动探测 Tailscale 地址 ----
if [ -z "${TAILNET_IP:-}" ]; then
  if command -v tailscale >/dev/null 2>&1; then
    TAILNET_IP=$(tailscale ip -4 2>/dev/null | head -1)
  fi
  [ -n "${TAILNET_IP:-}" ] || fail "无法自动探测 Tailscale 地址，请用 TAILNET_IP=100.x.x.x 指定"
fi
log "监听地址: $TAILNET_IP:$PORT"

# ---- 前置检查 ----
check_prereq() {
  local ok=1
  echo "=== 前置检查 ==="

  command -v nginx >/dev/null 2>&1 || { log "nginx: 未安装"; ok=0; }
  [ -d "/www/server/nginx" ] && log "nginx: /www/server/nginx"

  # PHP 的 pgsql 驱动是硬前提
  PHP_BIN="/www/server/php/$PHP_VER/bin/php"
  if [ -x "$PHP_BIN" ]; then
    for e in pgsql pdo_pgsql; do
      if $PHP_BIN -m 2>/dev/null | grep -qix "$e"; then
        log "PHP $PHP_VER $e: 已加载 ✅"
      else
        log "PHP $PHP_VER $e: 未加载 ❌  先跑 scripts/build-php-pgsql-extension.sh"
        ok=0
      fi
    done
  else
    log "PHP $PHP_VER: 未找到 $PHP_BIN"; ok=0
  fi

  # 端口占用
  if ss -tln 2>/dev/null | grep -q ":$PORT "; then
    if ss -tlnp 2>/dev/null | grep ":$PORT " | grep -q nginx; then
      log "端口 $PORT: 已被 nginx 占用（若是本站点则正常）"
    else
      log "端口 $PORT: 被其他进程占用 ❌"; ss -tlnp | grep ":$PORT " | sed 's/^/    /'; ok=0
    fi
  else
    log "端口 $PORT: 空闲 ✅"
  fi

  # htpasswd 工具（宝塔常缺）
  if command -v htpasswd >/dev/null 2>&1; then
    log "htpasswd: 可用"
  else
    log "htpasswd: 缺失，将改用 openssl passwd -apr1"
    command -v openssl >/dev/null 2>&1 || { log "openssl: 也缺失 ❌"; ok=0; }
  fi

  [ $ok -eq 1 ]
}

# ---- 生成/读取 Basic 密码 ----
gen_password() {
  if [ -n "${BASIC_PASSWORD:-}" ]; then
    log "使用外部传入的 BASIC_PASSWORD"
  elif [ -f "$PWFILE" ]; then
    BASIC_PASSWORD=$(cat "$PWFILE")
    log "复用已有的 $PWFILE"
  else
    # 20 位纯字母数字，避免 shell / nginx 转义问题
    BASIC_PASSWORD=$(tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 20)
    printf '%s' "$BASIC_PASSWORD" > "$PWFILE"
    chmod 600 "$PWFILE"
    log "已生成新密码并写入 $PWFILE（600）"
  fi
}

write_htpasswd() {
  # ⚠️ 关键坑：凭据文件【不能】放在 /www/server/panel/vhost/ 下。
  # 该目录及其 nginx 子目录权限是 drw------- (600)，nginx worker 以 www
  # 身份运行，无法穿透，会报：
  #   [crit] open() ".../xxx.htpasswd" failed (13: Permission denied)
  # 而且 `nginx -t` 是用 root 跑的，检查时【不会】报错 —— 只有真实请求才暴露，
  # 表现为「401 之后紧跟 500」。
  mkdir -p "$AUTHDIR"
  chmod 755 "$AUTHDIR"
  if command -v htpasswd >/dev/null 2>&1; then
    htpasswd -bc "$AUTHFILE" "$BASIC_USER" "$BASIC_PASSWORD" >/dev/null 2>&1
  else
    printf '%s:%s\n' "$BASIC_USER" "$(openssl passwd -apr1 "$BASIC_PASSWORD")" > "$AUTHFILE"
  fi
  chown root:www "$AUTHFILE" 2>/dev/null || true
  chmod 640 "$AUTHFILE"
  log "凭据文件: $AUTHFILE (640 root:www)"
}

write_conf() {
  cat > "$CONF" <<EOF
# Adminer for PostgreSQL —— 只监听 Tailscale 内网地址，公网不可达。
# 由 scripts/deploy-adminer.sh 生成，请勿手工编辑。
server {
    listen $TAILNET_IP:$PORT;
    server_name _;

    root $DOCROOT;
    index index.php;

    access_log /www/wwwlogs/adminer-tailnet.log;
    error_log  /www/wwwlogs/adminer-tailnet.error.log;

    # 第一层：HTTP Basic 认证
    auth_basic "Restricted";
    auth_basic_user_file $AUTHFILE;

    client_max_body_size 64m;

    # 禁止访问点文件与敏感后缀
    location ~ /\\. { deny all; }
    location ~* \\.(htpasswd|ini|log|bak|sql|tar|gz|zip)\$ { deny all; }

    location / {
        try_files \$uri \$uri/ /index.php;
    }

    location ~ \\.php\$ {
        include enable-php-$PHP_VER.conf;
    }
}
EOF
  log "站点配置: $CONF"
}

open_firewall() {
  echo
  echo "=== 防火墙 ==="
  if ! systemctl is-active --quiet firewalld 2>/dev/null; then
    log "firewalld 未运行，跳过"
    return 0
  fi
  # 只放行 Tailscale 网段（100.64.0.0/10 是 CGNAT 段，Tailscale 用它）
  RULE='rule family="ipv4" source address="100.64.0.0/10" port port="'"$PORT"'" protocol="tcp" accept'
  if firewall-cmd --zone=public --list-rich-rules 2>/dev/null | grep -q "port=\"$PORT\""; then
    log "规则已存在"
  else
    firewall-cmd --permanent --zone=public --add-rich-rule="$RULE" >/dev/null 2>&1
    firewall-cmd --reload >/dev/null 2>&1
    log "已放行 100.64.0.0/10 -> $PORT"
  fi
  log "回滚: firewall-cmd --permanent --zone=public --remove-rich-rule='$RULE' && firewall-cmd --reload"
}

verify() {
  echo
  echo "=== 安全检查 ==="
  local pass=0

  echo "  --- 监听范围（必须是 $TAILNET_IP，不能是 0.0.0.0） ---"
  ss -tlnp 2>/dev/null | grep ":$PORT " | sed 's/^/    /'
  if ss -tln 2>/dev/null | grep -q "$TAILNET_IP:$PORT "; then
    log "只监听 tailnet 地址 ✅"; pass=$((pass+1))
  else
    log "未按预期监听 ❌"
  fi

  echo "  --- 无认证应返回 401 ---"
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://$TAILNET_IP:$PORT/" 2>/dev/null)
  log "无认证 -> HTTP $code"
  [ "$code" = "401" ] && pass=$((pass+1))

  echo "  --- 带认证应返回 200 ---"
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 -u "$BASIC_USER:$BASIC_PASSWORD" "http://$TAILNET_IP:$PORT/" 2>/dev/null)
  log "带认证 -> HTTP $code"
  [ "$code" = "200" ] && pass=$((pass+1))

  echo "  --- 页面标题 ---"
  curl -s --max-time 15 -u "$BASIC_USER:$BASIC_PASSWORD" "http://$TAILNET_IP:$PORT/" 2>/dev/null \
    | grep -oE '<title>[^<]*</title>' | head -1 | sed 's/^/    /'

  echo "  --- 站点根目录不应有可执行的备份文件 ---"
  local extra
  extra=$(find "$DOCROOT" -maxdepth 1 -type f ! -name 'index.php' 2>/dev/null)
  if [ -n "$extra" ]; then
    log "发现多余文件（在网站根目录里的 .php 备份会被直接执行！）❌"
    echo "$extra" | sed 's/^/    /'
  else
    log "只有 index.php ✅"; pass=$((pass+1))
  fi

  echo "  --- 公网暴露面（需从外部确认，此处仅提示） ---"
  log "请从另一台机器确认公网 IP:$PORT 不通"

  echo
  log "通过 $pass 项检查"
}

case "$MODE" in
  --uninstall|uninstall)
    echo "=== 卸载 ==="
    rm -f "$CONF"
    nginx -t >/dev/null 2>&1 && nginx -s reload 2>/dev/null
    log "已删除 $CONF 并重载 nginx"
    log "保留 $DOCROOT 与 $AUTHDIR（如需彻底清理请手动 rm -rf）"
    exit 0
    ;;
  --verify|verify)
    check_prereq || true
    [ -f "$CONF" ] || fail "站点未部署"
    TAILNET_IP=$(grep -oP 'listen \K[0-9.]+' "$CONF" | head -1)
    BASIC_PASSWORD=$(cat "$PWFILE" 2>/dev/null)
    verify
    exit 0
    ;;
esac

check_prereq || fail "前置检查未通过"
gen_password

echo
echo "=== 1) 下载 Adminer $ADMINER_VERSION ==="
mkdir -p "$DOCROOT"
for url in \
  "https://github.com/vrana/adminer/releases/download/v$ADMINER_VERSION/adminer-$ADMINER_VERSION.php" \
  "https://www.adminer.org/static/download/$ADMINER_VERSION/adminer-$ADMINER_VERSION.php" ; do
  log "尝试 $url"
  if curl -fsSL --max-time 180 "$url" -o "$DOCROOT/index.php" 2>/dev/null; then
    sz=$(stat -c%s "$DOCROOT/index.php")
    if [ "$sz" -gt 100000 ]; then
      log "下载成功: $sz 字节"; break
    fi
    log "文件过小 ($sz)，继续尝试下一个源"
  else
    log "失败"
  fi
done
[ -s "$DOCROOT/index.php" ] || fail "Adminer 下载失败"
log "SHA256: $(sha256sum "$DOCROOT/index.php" | awk '{print $1}')"

# 语法检查（注意：这只验证语法，不验证运行时兼容性）
/www/server/php/$PHP_VER/bin/php -l "$DOCROOT/index.php" 2>&1 | sed 's/^/  /'

chown -R www:www "$DOCROOT"
chmod 644 "$DOCROOT/index.php"

echo
echo "=== 2) 配置认证 ==="
write_htpasswd

echo
echo "=== 3) 写 nginx 站点 ==="
write_conf

echo
echo "=== 4) 校验并重载 nginx ==="
nginx -t 2>&1 | sed 's/^/  /' || fail "nginx -t 未通过，已保留原配置"
nginx -s reload 2>&1 | sed 's/^/  /'
sleep 2

echo
echo "=== 5) 防火墙 ==="
open_firewall

verify

echo
echo "=== 部署完成 ==="
cat <<EOF

  网址:  http://$TAILNET_IP:$PORT

  第一层 HTTP Basic:
    用户名  $BASIC_USER
    密码    $BASIC_PASSWORD
    （也存于 $PWFILE，权限 600）

  第二层 Adminer 登录表单:
    System    PostgreSQL
    Server    127.0.0.1
    Username  见 .env 的 DATABASE_URL 中内嵌的用户
    Password  见 .env 的 DATABASE_URL 中内嵌的密码
    Database  knowledge_base

  ⚠️ 注意：真实 .env 里【没有】POSTGRES_PASSWORD 这个独立变量，
     密码是内嵌在 DATABASE_URL 里的。.env.example 里的 POSTGRES_PASSWORD
     是示例占位值，用它登录会认证失败。

  ⚠️ 连续登录失败会被 Adminer 静默锁定（不报错，只退回登录页）：
     rm -f /tmp/adminer-invalid

  回滚:
     sudo bash \$0 --uninstall
EOF
