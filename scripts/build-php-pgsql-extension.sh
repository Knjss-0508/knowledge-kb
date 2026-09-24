#!/bin/bash
# 为宝塔面板的 PHP 编译 PostgreSQL 驱动扩展（pgsql / pdo_pgsql）。
#
# 背景：宝塔安装的 PHP 默认【不含】PostgreSQL 驱动，只有 pdo_mysql / pdo_sqlite。
# 因此任何 PHP 版 PostgreSQL 工具（Adminer、phpPgAdmin）都跑不起来，
# 表现为连接时报找不到驱动。
#
# 用法：
#   sudo bash scripts/build-php-pgsql-extension.sh            # 编译并安装
#   sudo bash scripts/build-php-pgsql-extension.sh --verify   # 只检查是否已装
#
# 环境变量：
#   PHP_VER      宝塔 PHP 版本号，默认 74（对应 /www/server/php/74）
#   PHP_SRC_VER  PHP 源码版本，默认自动从 php -v 读取
#   PG_DIR       PostgreSQL 安装目录，默认 /www/server/pgsql
#
# 注意：安装后需要重启 PHP-FPM 才会生效，期间其他 PHP 站点会瞬断约 1 秒。
set -u

PHP_VER="${PHP_VER:-74}"
PG_DIR="${PG_DIR:-/www/server/pgsql}"
PHP_ROOT="/www/server/php/$PHP_VER"
PHP_BIN="$PHP_ROOT/bin/php"
PHP_CONFIG="$PHP_ROOT/bin/php-config"
PHPIZE="$PHP_ROOT/bin/phpize"
MODE="${1:-install}"

log()  { printf '  %s\n' "$*"; }
fail() { printf '  [错误] %s\n' "$*" >&2; exit 1; }

echo "=== 0) 前置检查 ==="
[ -x "$PHP_BIN" ]    || fail "找不到 $PHP_BIN"
[ -x "$PHP_CONFIG" ] || fail "找不到 $PHP_CONFIG（宝塔 PHP 是否装全？）"
[ -x "$PHPIZE" ]     || fail "找不到 $PHPIZE"
[ -x "$PG_DIR/bin/pg_config" ] || fail "找不到 $PG_DIR/bin/pg_config"

PHP_SRC_VER="${PHP_SRC_VER:-$($PHP_BIN -r 'echo PHP_VERSION;' 2>/dev/null)}"
EXTDIR=$($PHP_CONFIG --extension-dir 2>/dev/null)
[ -n "$EXTDIR" ] || fail "无法取得扩展目录"

log "PHP:        $PHP_SRC_VER ($PHP_ROOT)"
log "扩展目录:   $EXTDIR"
log "PostgreSQL: $($PG_DIR/bin/pg_config --version)"
log "pg include: $($PG_DIR/bin/pg_config --includedir)"
log "pg lib:     $($PG_DIR/bin/pg_config --libdir)"

# ini 文件：FPM 用 php.ini，CLI 用 php-cli.ini，两个都要写
INI_FILES=""
for f in "$PHP_ROOT/etc/php.ini" "$PHP_ROOT/etc/php-cli.ini"; do
  [ -f "$f" ] && INI_FILES="$INI_FILES $f"
done
log "ini 文件:  $INI_FILES"

if [ "$MODE" = "--verify" ] || [ "$MODE" = "verify" ]; then
  echo
  echo "=== 当前状态 ==="
  for e in pgsql pdo_pgsql; do
    if $PHP_BIN -m 2>/dev/null | grep -qix "$e"; then
      log "$e: 已加载 (版本 $($PHP_BIN -r "echo phpversion('$e');" 2>/dev/null))"
    else
      log "$e: 未加载"
    fi
  done
  log "--- 扩展文件 ---"
  ls -la "$EXTDIR"/*pgsql*.so 2>/dev/null | sed 's/^/    /' || log "(无)"
  exit 0
fi

# 编译工具链
echo
echo "=== 1) 编译工具链 ==="
for t in gcc make autoconf; do
  command -v "$t" >/dev/null 2>&1 || fail "缺少 $t，请先安装编译工具链"
  log "$t: $(command -v "$t")"
done

echo
echo "=== 2) 下载 PHP $PHP_SRC_VER 源码 ==="
cd /tmp
TARBALL="php-$PHP_SRC_VER.tar.gz"
if [ ! -f "$TARBALL" ]; then
  log "下载 $TARBALL ..."
  curl -fsSL --max-time 300 -o "$TARBALL" \
    "https://www.php.net/distributions/php-$PHP_SRC_VER.tar.gz" \
    || fail "下载失败。可手动下载后放到 /tmp/$TARBALL 再重跑"
fi
log "大小: $(stat -c%s "$TARBALL") 字节"
tar tzf "$TARBALL" >/dev/null 2>&1 || fail "tar 包损坏"
log "tar 校验通过"

echo
echo "=== 3) 解压扩展源码 ==="
SRCDIR="/tmp/php-$PHP_SRC_VER"
rm -rf "$SRCDIR"
tar xzf "$TARBALL" "php-$PHP_SRC_VER/ext/pgsql" "php-$PHP_SRC_VER/ext/pdo_pgsql" 2>/dev/null \
  || fail "解压失败"
log "$SRCDIR/ext/pgsql"
log "$SRCDIR/ext/pdo_pgsql"

# 编译单个扩展
build_ext() {
  local name="$1"; shift
  echo
  echo "=== 编译 $name ==="
  cd "$SRCDIR/ext/$name" || return 1
  make clean >/dev/null 2>&1 || true
  "$PHPIZE" >/dev/null 2>&1

  # 关键：--with-* 必须传【目录】而不是 pg_config 全路径。
  # PHP 7.4 的 config.m4 里是：
  #   AC_PATH_PROG(PGCONFIG, pg_config, no, $PHP_PGSQL/bin:$PATH)
  # 传全路径会让它去找 <路径>/bin/pg_config，必然失败。
  if ! ./configure --with-php-config="$PHP_CONFIG" "$@" >"/tmp/build-$name.log" 2>&1; then
    log "configure 失败，关键行："
    grep -iE "error|cannot find|not found" "/tmp/build-$name.log" | tail -10 | sed 's/^/    /'
    return 1
  fi
  log "configure OK (pg_config: $(grep -m1 'checking for pg_config' "/tmp/build-$name.log" | sed 's/.*\.\.\. //'))"

  if ! make -j"$(nproc)" >>"/tmp/build-$name.log" 2>&1; then
    log "make 失败，尾部："; tail -25 "/tmp/build-$name.log" | sed 's/^/    /'; return 1
  fi
  if ! make install >>"/tmp/build-$name.log" 2>&1; then
    log "make install 失败"; tail -15 "/tmp/build-$name.log" | sed 's/^/    /'; return 1
  fi
  log "编译安装完成"
  return 0
}

R1=0; R2=0
# pdo_pgsql 与 pgsql 都要编：Adminer 实际用的是 pgsql 扩展
build_ext pdo_pgsql "--with-pdo-pgsql=$PG_DIR" || R1=$?
build_ext pgsql     "--with-pgsql=$PG_DIR"     || R2=$?

echo
echo "=== 4) 编译结果 ==="
[ $R1 -eq 0 ] && log "pdo_pgsql: 成功" || log "pdo_pgsql: 失败"
[ $R2 -eq 0 ] && log "pgsql:     成功" || log "pgsql:     失败"
[ $R1 -eq 0 ] && [ $R2 -eq 0 ] || fail "有扩展编译失败"

ls -la "$EXTDIR"/*pgsql*.so 2>/dev/null | sed 's/^/  /'

echo
echo "=== 5) 依赖解析检查（必须能解析到 libpq） ==="
for so in "$EXTDIR"/pdo_pgsql.so "$EXTDIR"/pgsql.so; do
  [ -f "$so" ] || continue
  log "--- $(basename "$so") ---"
  ldd "$so" 2>/dev/null | grep -iE "libpq|not found" | sed 's/^/    /'
done

echo
echo "=== 6) 写入 ini ==="
for ini in $INI_FILES; do
  cp -a "$ini" "${ini}.bak-pgsql-$(date +%Y%m%d-%H%M%S)"
  for e in pdo_pgsql pgsql; do
    [ -f "$EXTDIR/$e.so" ] || continue
    if grep -q "^extension=$e.so" "$ini" 2>/dev/null; then
      log "$ini: $e.so 已存在"
    else
      printf '\nextension=%s.so\n' "$e" >> "$ini"
      log "$ini: 已追加 $e.so"
    fi
  done
done

echo
echo "=== 7) CLI 验证（不影响运行中的服务） ==="
for e in pgsql pdo_pgsql; do
  if $PHP_BIN -m 2>/dev/null | grep -qix "$e"; then
    log "$e: 已加载"
  else
    log "$e: 未加载 ❌"
  fi
done

echo
echo "=== 8) 重启 PHP-FPM ==="
FPM=""
for c in "/etc/init.d/php-fpm-$PHP_VER" "/etc/init.d/php-fpm-$(echo "$PHP_VER" | sed 's/^\(.\)/\1./')"; do
  [ -x "$c" ] && FPM="$c" && break
done
if [ -n "$FPM" ]; then
  log "使用 $FPM（其他 PHP 站点会瞬断约 1 秒）"
  "$FPM" restart 2>&1 | sed 's/^/    /'
  sleep 3
  ps -eo pid,args --no-headers 2>/dev/null | grep "[p]hp-fpm: master" | sed 's/^/    运行中: /'
else
  log "未找到 init 脚本，请手动重启 PHP-FPM"
fi

echo
echo "=== 完成 ==="
echo "  验证 FPM 侧是否真的加载（放一个探针到站点根目录再请求）："
echo "     <?php var_dump(extension_loaded('pgsql'));"
echo
echo "  ⚠️ 不要用宝塔面板改 PHP 设置 —— 面板可能重写 php.ini，会丢掉这两行。"
echo "     改完务必检查： $PHP_BIN -m | grep -i pgsql"
