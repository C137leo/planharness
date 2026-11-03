#!/usr/bin/env bash
# ============================================================
# 建库建用户（调 deploy/init-db.sql）
# ------------------------------------------------------------
# 用法：
#     sudo deploy/create-db.sh --password '强密码'
#     sudo deploy/create-db.sh --password "$(openssl rand -base64 24)"
#
# 可选参数：
#     --user        appuser         应用角色名
#     --db          planharness     库名
#     --superuser   postgres        超级用户（执行 DDL 用）
#     --host        127.0.0.1       默认本地 socket；跨机器时指定
#     --port        5432
#     --print-env                   建完后打印可直接粘贴的 DATABASE_URL
# ============================================================
set -euo pipefail

DB_PASSWORD=""
APP_USER="appuser"
APP_DB="planharness"
SUPERUSER="postgres"
PG_HOST=""
PG_PORT=""
PRINT_ENV=0

usage() {
    sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --password)  DB_PASSWORD="${2:-}"; shift 2 ;;
        --user)      APP_USER="${2:-}"; shift 2 ;;
        --db)        APP_DB="${2:-}"; shift 2 ;;
        --superuser) SUPERUSER="${2:-}"; shift 2 ;;
        --host)      PG_HOST="${2:-}"; shift 2 ;;
        --port)      PG_PORT="${2:-}"; shift 2 ;;
        --print-env) PRINT_ENV=1; shift ;;
        -h|--help)   usage; exit 0 ;;
        *) echo "未知参数: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [ -z "$DB_PASSWORD" ]; then
    echo "错误：必须用 --password 指定数据库密码" >&2
    exit 2
fi
case "$DB_PASSWORD" in
    *CHANGE_ME*|*change-me*) echo "错误：密码不能是占位符" >&2; exit 2 ;;
esac

SQL="$(dirname "$0")/init-db.sql"
[ -f "$SQL" ] || { echo "错误：找不到 $SQL" >&2; exit 1; }

PSQL_ARGS=(-v ON_ERROR_STOP=1 -U "$SUPERUSER"
           -v "app_password=$DB_PASSWORD"
           -v "app_user=$APP_USER"
           -v "app_db=$APP_DB")
[ -n "$PG_HOST" ] && PSQL_ARGS+=(-h "$PG_HOST")
[ -n "$PG_PORT" ] && PSQL_ARGS+=(-p "$PG_PORT")

echo "→ 执行 $SQL（超级用户 $SUPERUSER，库 $APP_DB，角色 $APP_USER）"
psql "${PSQL_ARGS[@]}" -f "$SQL"

if [ "$PRINT_ENV" -eq 1 ]; then
    ENC="$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1], safe=""))' "$DB_PASSWORD")"
    HOST_FOR_URL="${PG_HOST:-127.0.0.1}"
    PORT_FOR_URL="${PG_PORT:-5432}"
    echo
    echo "把下面这行写进 /etc/planharness/planharness.env："
    echo "DATABASE_URL=postgresql+psycopg://${APP_USER}:${ENC}@${HOST_FOR_URL}:${PORT_FOR_URL}/${APP_DB}"
fi

echo
echo "✓ 完成。连接自检："
echo "  psql \"postgresql://${APP_USER}:<密码>@${HOST_FOR_URL:-127.0.0.1}:${PORT_FOR_URL:-5432}/${APP_DB}\" -c 'select 1'"
