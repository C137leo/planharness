#!/usr/bin/env bash
# ============================================================
# PlanHarness 上线前自检
# ------------------------------------------------------------
# 把「上线清单」变成可执行检查：阻断项失败 → 退出码 1，不要上线。
#
# 用法（服务器上，root）：
#     sudo /opt/planharness/deploy/preflight.sh
#     sudo ENV_FILE=/etc/planharness/planharness.env /opt/planharness/deploy/preflight.sh
#
# 本地预演（用 backend/.env）：
#     ENV_FILE=backend/.env deploy/preflight.sh --soft
#
# --soft：只打印结果，不因失败退出非 0（本地演练用）
# ============================================================
set -uo pipefail

ENV_FILE="${ENV_FILE:-/etc/planharness/planharness.env}"
SOFT=0
[ "${1:-}" = "--soft" ] && SOFT=1

FAILED=0
WARNED=0

pass() { printf '  \033[32m✓\033[0m %s\n' "$*"; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$*"; FAILED=$((FAILED+1)); }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; WARNED=$((WARNED+1)); }
head_() { printf '\n\033[1m%s\033[0m\n' "$*"; }

# ---------- 读取环境文件 ----------
head_ "1) 环境文件"
if [ "$ENV_FILE" != "/etc/planharness/planharness.env" ]; then
    printf '  (本地演练模式：ENV_FILE=%s，部分检查只作参考)\n' "$ENV_FILE"
fi
if [ ! -f "$ENV_FILE" ]; then
    fail "找不到环境文件：$ENV_FILE"
else
    pass "存在：$ENV_FILE"
    perms="$(stat -c '%a %U:%G' "$ENV_FILE" 2>/dev/null \
        || stat -f '%Lp %Su:%Sg' "$ENV_FILE" 2>/dev/null || echo '?')"
    PROD_ENV_FILE="/etc/planharness/planharness.env"
    if [ "$ENV_FILE" != "$PROD_ENV_FILE" ]; then
        # 本地演练：开发机的 .env 属于你自己，644 很正常，不当作问题
        warn "权限 $perms（本地文件，无需调整；生产文件 $PROD_ENV_FILE 才要求 640 root:planharness）"
    else
        case "$perms" in
            "640 root:planharness") pass "权限 $perms" ;;
            600*|640*)              warn "权限 $perms（建议 640 root:planharness）" ;;
            *)                      fail "权限 $perms（应为 640 root:planharness）" ;;
        esac
    fi
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE" 2>/dev/null || fail "环境文件解析失败"
    set +a
fi

# ---------- 配置项 ----------
head_ "2) 关键配置"
[ "${ENV:-dev}" = "prod" ] && pass "ENV=prod" || fail "ENV=${ENV:-未设置}（生产必须 prod）"

case "${DATABASE_URL:-}" in
    "") fail "DATABASE_URL 未设置" ;;
    *CHANGE_ME*|*change-me*)
        fail "DATABASE_URL 里还有 CHANGE_ME 占位符（密码没填）" ;;
    postgresql*) pass "DATABASE_URL 指向 Postgres" ;;
    *)           fail "DATABASE_URL 不是 Postgres：${DATABASE_URL%%:*}" ;;
esac

SK="${SECRET_KEY:-}"
if [ -z "$SK" ]; then
    fail "SECRET_KEY 未设置"
elif echo "$SK" | grep -qiE 'change[-_]?me|placeholder|CHANGE'; then
    fail "SECRET_KEY 还是占位符（任何人可伪造 token）"
elif [ "${#SK}" -lt 32 ]; then
    fail "SECRET_KEY 太短（${#SK} < 32）"
else
    pass "SECRET_KEY 已设置（${#SK} 字节）"
fi

case "${CORS_ORIGINS:-}" in
    *https://*) pass "CORS 含 https 域名" ;;
    *)          warn "CORS_ORIGINS 不含 https 域名：${CORS_ORIGINS:-未设置}" ;;
esac

if [ -z "${DEEPSEEK_API_KEY:-}" ]; then
    warn "DEEPSEEK_API_KEY 为空（走模板兜底，可接受）"
elif echo "${DEEPSEEK_API_KEY}" | grep -q '^sk-'; then
    pass "DEEPSEEK_API_KEY 已配置"
else
    warn "DEEPSEEK_API_KEY 存在但不像 sk- 开头"
fi

[ "${LANGGRAPH_STRICT_MSGPACK:-}" = "true" ] \
    && pass "LANGGRAPH_STRICT_MSGPACK=true" \
    || warn "未设 LANGGRAPH_STRICT_MSGPACK=true（数据库被攻破时可被反序列化攻击）"

# ---------- 端口暴露 ----------
head_ "3) 端口只绑本机"
if command -v ss >/dev/null 2>&1; then
    for port in 9200 5432; do
        line="$(ss -lntp 2>/dev/null | grep ":${port} " | head -1)"
        if [ -z "$line" ]; then
            warn "${port} 没有监听（服务未启动？）"
        elif echo "$line" | grep -qE '127\.0\.0\.1:'"${port}"'|\[::1\]:'"${port}"; then
            pass "${port} 仅本机监听"
        else
            fail "${port} 对公网开放：$line"
        fi
    done
else
    warn "没有 ss 命令，跳过端口检查"
fi

# ---------- 仓库卫生 ----------
head_ "4) 仓库卫生"
REPO="${REPO_DIR:-/opt/planharness}"
if [ -d "$REPO/.git" ]; then
    if git -C "$REPO" ls-files --error-unmatch .env >/dev/null 2>&1; then
        fail ".env 被 Git 跟踪了"
    else
        pass ".env 未被 Git 跟踪"
    fi
    # 只扫「被 Git 跟踪的文件」：.env 是忽略文件，本地存在密钥是正常的
    if git -C "$REPO" grep -I -l -E 'sk-[A-Za-z0-9]{20,}' >/dev/null 2>&1; then
        fail "Git 跟踪的文件里出现疑似真实 Key（sk- 开头）："
        git -C "$REPO" grep -I -l -E 'sk-[A-Za-z0-9]{20,}' | sed 's/^/       /'
    else
        pass "Git 跟踪的文件里无 sk- 明文"
    fi
else
    warn "$REPO 不是 git 仓库，跳过"
fi

# ---------- 定时任务 ----------
head_ "5) 定时任务"
if command -v systemctl >/dev/null 2>&1; then
    for unit in planharness-backup.timer planharness-checkpoint-cleanup.timer; do
        if systemctl is-enabled --quiet "$unit" 2>/dev/null; then
            pass "$unit 已启用"
        else
            warn "$unit 未启用"
        fi
    done
else
    warn "没有 systemctl，跳过"
fi

# ---------- 结论 ----------
head_ "结论"
if [ "$FAILED" -gt 0 ]; then
    printf '\033[31m阻断项 %d 个，警告 %d 个 —— 不要上线\033[0m\n' "$FAILED" "$WARNED"
    [ "$SOFT" -eq 1 ] && exit 0
    exit 1
fi
printf '\033[32m阻断项全部通过（警告 %d 个）\033[0m\n' "$WARNED"
exit 0
