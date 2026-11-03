#!/usr/bin/env bash
# ============================================================
# PlanHarness 数据库每日备份
# ------------------------------------------------------------
# 为什么必须有：权限收紧只能降低「被攻破的概率」，
# 真正保证「被攻破了也不报废」的是**能恢复到某个时间点**。
#
# 用法（root 或能 docker exec 的用户执行）：
#     sudo /opt/planharness/deploy/db-backup.sh
#
# 可用环境变量覆盖：
#     BACKUP_DIR  /var/backups/planharness
#     KEEP_DAYS   14
#     PG_CONTAINER pg          # 容器名，deploy/README 里用的是 pg
#     PG_DATABASE planharness
#     PG_USER     appuser
#
# 说明：
#   - 走容器内的 Unix socket，官方 postgres 镜像对 local 连接是 trust，不需要密码；
#   - --no-owner / --no-privileges：换机器恢复时不依赖角色名；
#   - 备份完立刻做一次 gzip -t 校验，避免留下坏包。
# ============================================================
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/var/backups/planharness}"
KEEP_DAYS="${KEEP_DAYS:-14}"
PG_CONTAINER="${PG_CONTAINER:-pg}"
PG_DATABASE="${PG_DATABASE:-planharness}"
PG_USER="${PG_USER:-appuser}"

STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="${BACKUP_DIR}/planharness-${STAMP}.sql.gz"

log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*"; }

mkdir -p "$BACKUP_DIR"

if ! docker ps --format '{{.Names}}' | grep -qx "$PG_CONTAINER"; then
    log "ERROR: 容器 ${PG_CONTAINER} 未运行，备份中止"
    exit 1
fi

log "开始备份 ${PG_DATABASE} → ${OUT}"
# 先写临时文件，成功后再改名，避免半截文件被当成有效备份
TMP="${OUT}.part"
if ! docker exec -i "$PG_CONTAINER" \
        pg_dump -U "$PG_USER" -d "$PG_DATABASE" --no-owner --no-privileges \
        | gzip -9 > "$TMP"; then
    log "ERROR: pg_dump 失败"
    rm -f "$TMP"
    exit 1
fi

if [ ! -s "$TMP" ] || ! gzip -t "$TMP" 2>/dev/null; then
    log "ERROR: 备份文件为空或损坏"
    rm -f "$TMP"
    exit 1
fi

mv "$TMP" "$OUT"
SIZE="$(du -h "$OUT" | cut -f1)"
log "备份完成：${OUT}（${SIZE}）"

# 清理过期备份
find "$BACKUP_DIR" -maxdepth 1 -name 'planharness-*.sql.gz' -mtime "+${KEEP_DAYS}" -print -delete \
    | while read -r f; do log "已删除过期备份：$f"; done

log "当前保留："
ls -1t "$BACKUP_DIR"/planharness-*.sql.gz 2>/dev/null | head -5 | while read -r f; do
    log "  $(basename "$f") $(du -h "$f" | cut -f1)"
done

log "恢复方法（演练时才执行）："
log "  gunzip -c ${OUT} | docker exec -i ${PG_CONTAINER} psql -U ${PG_USER} -d ${PG_DATABASE}"
