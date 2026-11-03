#!/usr/bin/env bash
# ============================================================
# 生成生产环境文件（/etc/planharness/planharness.env）
# ------------------------------------------------------------
# 用法（服务器上，root）：
#     sudo deploy/make-prod-env.sh --db-password '数据库密码'
#
# 它会：
#   1) 生成 48 字节随机 SECRET_KEY（不需要你手动跑 python）
#   2) 把密码做 URL 编码后拼进 DATABASE_URL（@ : / 之类不会破坏连接串）
#   3) 写文件并设成 640 root:planharness
#   4) 打印一份打码后的摘要，方便核对
#
# 可选参数：
#     --db-password  必填，数据库密码
#     --db-user      appuser
#     --db-name      planharness
#     --db-host      127.0.0.1
#     --db-port      5432
#     --domain       www.agentworkout.online
#     --deepseek-key sk-xxx（可留空，留空走模板兜底）
#     --out          输出路径，默认 /etc/planharness/planharness.env
#     --force        已存在时覆盖
# ============================================================
set -euo pipefail

OUT="${OUT:-/etc/planharness/planharness.env}"
DB_PASSWORD=""
DB_USER="appuser"
DB_NAME="planharness"
DB_HOST="127.0.0.1"
DB_PORT="5432"
DOMAIN="www.agentworkout.online"
DEEPSEEK_KEY=""
DEEPSEEK_MODEL="deepseek-flash"
FORCE=0

usage() { sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
    case "$1" in
        --db-password)  DB_PASSWORD="${2:-}"; shift 2 ;;
        --db-user)      DB_USER="${2:-}"; shift 2 ;;
        --db-name)      DB_NAME="${2:-}"; shift 2 ;;
        --db-host)      DB_HOST="${2:-}"; shift 2 ;;
        --db-port)      DB_PORT="${2:-}"; shift 2 ;;
        --domain)       DOMAIN="${2:-}"; shift 2 ;;
        --deepseek-key) DEEPSEEK_KEY="${2:-}"; shift 2 ;;
        --out)          OUT="${2:-}"; shift 2 ;;
        --force)        FORCE=1; shift ;;
        -h|--help)      usage; exit 0 ;;
        *) echo "未知参数: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [ -z "$DB_PASSWORD" ]; then
    echo "错误：必须用 --db-password 指定数据库密码" >&2
    exit 2
fi
case "$DB_PASSWORD" in
    *CHANGE_ME*|*change-me*) echo "错误：密码不能是占位符" >&2; exit 2 ;;
esac

if [ -e "$OUT" ] && [ "$FORCE" -eq 0 ]; then
    echo "错误：$OUT 已存在；确认要覆盖请加 --force" >&2
    exit 1
fi

# 密码里的 @ : / ? # 会破坏 URL，必须编码
ENCODED_PW="$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1], safe=""))' "$DB_PASSWORD")"
SECRET_KEY="$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')"

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

cat > "$TMP" <<EOF
# ============================================================
# PlanHarness 生产环境变量（systemd EnvironmentFile）
# 由 deploy/make-prod-env.sh 生成于 $(date '+%F %T')
# 权限必须 640 root:planharness；不要提交到 Git。
# ============================================================

ENV=prod
DATABASE_URL=postgresql+psycopg://${DB_USER}:${ENCODED_PW}@${DB_HOST}:${DB_PORT}/${DB_NAME}
SECRET_KEY=${SECRET_KEY}
CORS_ORIGINS=https://${DOMAIN}

DEEPSEEK_API_KEY=${DEEPSEEK_KEY}
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
DEEPSEEK_MODEL=${DEEPSEEK_MODEL}
DEEPSEEK_TIMEOUT_S=30

CONTEXT_TOKEN_BUDGET=2000
CHECKPOINTER_BACKEND=auto
CHECKPOINTER_POOL_SIZE=5
LANGGRAPH_STRICT_MSGPACK=true
EOF

# 建目录 + 落文件 + 定权限
mkdir -p "$(dirname "$OUT")"
install -m 640 "$TMP" "$OUT"

if [ "$(id -u)" -eq 0 ]; then
    # 组不存在时创建，避免 chown 失败
    getent group planharness >/dev/null 2>&1 || groupadd --system planharness
    chown root:planharness "$OUT"
fi

echo "✓ 已写入 $OUT"
echo
echo "摘要（值已打码）："
printf '  %-22s %s\n' ENV "prod"
printf '  %-22s %s\n' DATABASE_URL "postgresql+psycopg://${DB_USER}:****@${DB_HOST}:${DB_PORT}/${DB_NAME}"
printf '  %-22s %s\n' SECRET_KEY "已生成（${#SECRET_KEY} 字符）"
printf '  %-22s %s\n' CORS_ORIGINS "https://${DOMAIN}"
printf '  %-22s %s\n' DEEPSEEK_API_KEY "${DEEPSEEK_KEY:-（空，走模板兜底）}"
[ "$(id -u)" -eq 0 ] && printf '  %-22s %s\n' 权限 "$(stat -c '%a %U:%G' "$OUT")"
echo
echo "下一步："
echo "  1) sudo ENV_FILE=$OUT $(dirname "$0")/preflight.sh"
echo "  2) sudo systemctl restart planharness"
