#!/usr/bin/env bash
# 把 healthy-fitness-coach skill 收进镜像（vendor）。
#
# 为什么 vendor 而不是部署时再 clone：
#   - 生产机不一定有外网/Git 权限；
#   - 版本必须和代码一起固定，避免"昨天还能生成、今天 skill 改了"；
#   - skill 是 MIT 协议、纯 Node（无 npm 依赖，1.3MB 核心文件），适合直接打包。
#
# 用法：
#   ./deploy/vendor-skill.sh                       # 默认从本机路径复制
#   ./deploy/vendor-skill.sh /path/to/skill-repo   # 指定 skill 仓库
#
# 复制完成后会重跑动作目录同步（Python 侧要用 302 个动作的静态快照）。
set -euo pipefail

SRC="${1:-${SKILL_DIR:-$HOME/PycharmProjects/healthy-fitness-coach-skill}}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$ROOT/backend/vendor/healthy-fitness-coach-skill"

if [ ! -f "$SRC/agent-integration/coach.js" ]; then
  echo "找不到 skill：$SRC（需要 agent-integration/coach.js）" >&2
  exit 1
fi

echo "skill 源：$SRC"
rm -rf "$DEST"
mkdir -p "$DEST"
# 排除 .git / node_modules / 大图片缓存，保留运行需要的 JS 与资源
tar -C "$SRC" \
  --exclude='.git' --exclude='node_modules' --exclude='.venv' \
  -cf - . | tar -C "$DEST" -xf -

# 记录版本：有 git 就记 commit，没有就记时间
if git -C "$SRC" rev-parse --short HEAD >/dev/null 2>&1; then
  git -C "$SRC" rev-parse HEAD > "$DEST/SKILL_COMMIT"
else
  date -u +"%Y-%m-%dT%H:%M:%SZ" > "$DEST/SKILL_COMMIT"
fi

echo "已复制到：$DEST（$(du -sh "$DEST" | cut -f1)）"
echo "版本：$(cat "$DEST/SKILL_COMMIT")"

# 动作目录是生成后提交的静态快照，skill 更新后必须重跑
cd "$ROOT/backend"
if [ -x .venv/bin/python ]; then
  SKILL_DIR="$DEST" .venv/bin/python scripts/sync_exercise_catalog.py
else
  echo "跳过动作目录同步：backend/.venv/bin/python 不存在" >&2
fi
