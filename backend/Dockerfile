FROM python:3.12-slim

# skill 是 Node 写的确定性引擎，容器里必须有 node；路径由 deploy/vendor-skill.sh 生成
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SKILL_DIR=/app/vendor/healthy-fitness-coach-skill \
    SKILL_TIMEOUT=20

# 装 node：skill 只用 Node 内置模块（fs/path/crypto），不需要 npm install
RUN apt-get update \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
# vendor 目录由 deploy/vendor-skill.sh 生成（含 .gitkeep 占位，空目录也能构建）
COPY vendor ./vendor
RUN pip install --no-cache-dir .

# 镜像自检：打印 node 版本；skill 没 vendor 时只告警（运行时会走本地兜底）
RUN node --version \
 && test -f /app/vendor/healthy-fitness-coach-skill/agent-integration/coach.js \
 || echo "WARN: skill 未 vendor，运行时会走本地兜底（见 docs/32）"

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
