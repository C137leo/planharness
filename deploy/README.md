# PlanHarness 生产部署：systemd + Nginx + 现有 Postgres 容器

> 适用当前服务器：Ubuntu 24.04，2C2G，已有 Nginx，已有 `pg` 容器，API 不跑容器。  
> **不要运行 `docker-compose.example.yml`**：它会再起 Postgres/Caddy，和现有 Nginx/端口冲突。

## 1. 结论

你的架构是合理的：

```text
浏览器 → Nginx :80/:443 → uvicorn 127.0.0.1:9200 → Postgres 127.0.0.1:5432
```

- 只暴露 80/443，正确。
- API 绑 127.0.0.1，正确。
- Postgres 只映射 127.0.0.1，正确。
- 2C2G 不适合再跑 Caddy + API 容器，复用宿主 systemd 更稳。

需要修正的重点：

1. **API 不要用 root 跑**；给它专用用户 `planharness`。
2. `.env` 放到 `/etc/planharness/planharness.env`，`640 root:planharness`。
3. `DATABASE_URL` 用现有 `appuser`，不是 compose 里的 `planharness` 用户。
4. systemd 加 `Restart=always`、`NoNewPrivileges`、`ProtectSystem`、`ProtectHome`。
5. Nginx 加 `X-Forwarded-*`、120s 读超时、限流。
6. uvicorn 只开 **1 worker**，2C2G 不要开多 worker。

## 2. 先别动：过 22:00 Gate

今晚先保证现有服务能跑：

```bash
systemctl status <当前服务名> --no-pager
journalctl -u <当前服务名> -n 100 --no-pager
curl -sS https://www.agentworkout.online/api/v1/health
```

Gate 过了，明天上午再做非 root 迁移，避免今晚把可用服务弄挂。

## 3. 迁移步骤

### 3.1 建用户和目录

```bash
sudo useradd --system --home /opt/planharness --shell /usr/sbin/nologin planharness || true
sudo mkdir -p /opt/planharness /etc/planharness
sudo chown -R planharness:planharness /opt/planharness
```

### 3.2 停旧服务并复制代码

```bash
sudo systemctl stop <当前服务名>
sudo rsync -a --exclude '.venv' --exclude '.env' --exclude '__pycache__' \
  /root/planharness/ /opt/planharness/
sudo chown -R planharness:planharness /opt/planharness
```

### 3.3 重建 venv（用 planharness 用户）

```bash
sudo -u planharness python3.12 -m venv /opt/planharness/backend/.venv
sudo -u planharness /opt/planharness/backend/.venv/bin/pip install --upgrade pip
sudo -u planharness /opt/planharness/backend/.venv/bin/pip install /opt/planharness/backend
```

> 如果你用 uv：`sudo -u planharness uv sync`，确保 uv 对 planharness 用户可用。

### 3.4 环境文件

```bash
sudo install -m 640 -o root -g planharness /dev/null /etc/planharness/planharness.env
sudo nano /etc/planharness/planharness.env
```

内容：

```text
ENV=prod
DATABASE_URL=postgresql+psycopg://appuser:你的密码@127.0.0.1:5432/planharness
SECRET_KEY=至少32字节随机字符串
CORS_ORIGINS=https://www.agentworkout.online
DEEPSEEK_API_KEY=你的Key
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
DEEPSEEK_MODEL=deepseek-chat
DEEPSEEK_TIMEOUT_S=30
```

注意：

- 密码里如果有 `@ : /`，先 URL 编码。
- `chmod 640`，owner `root:planharness`；systemd 以 root 读取 EnvironmentFile，再降权给 planharness。
- 不要把 `.env` 放回代码目录，也不要提交 Git。

### 3.5 systemd

```bash
sudo cp deploy/planharness.service /etc/systemd/system/planharness.service
sudo systemctl daemon-reload
sudo systemctl enable --now planharness
sudo systemctl status planharness --no-pager
```

如果 `ProtectSystem=strict` 导致应用无法写缓存/临时文件，先看日志：

```bash
journalctl -u planharness -n 100 --no-pager
```

需要写路径时再加：

```ini
ReadWritePaths=/opt/planharness/backend
```

### 3.6 Nginx

```bash
sudo cp deploy/nginx-planharness.conf /etc/nginx/conf.d/planharness.conf
sudo nginx -t
sudo systemctl reload nginx
```

如果已有 `server_name www.agentworkout.online` 的配置，先合并/删除旧的，避免重复 server 块。

证书路径默认：

```text
/etc/letsencrypt/live/www.agentworkout.online/fullchain.pem
/etc/letsencrypt/live/www.agentworkout.online/privkey.pem
```

没有证书：

```bash
sudo certbot --nginx -d www.agentworkout.online
```

### 3.7 静态 H5 权限

```bash
sudo chmod -R a+rX /opt/planharness/frontend/dist
```

Nginx 以 `www-data` 读取；目录需要 `x`，文件需要 `r`。

## 4. 验证

```bash
# 只应看到 80/443 对公网，9200/5432 只在 127.0.0.1
sudo ss -lntp | grep -E ':(80|443|9200|5432)\b'

# API 服务
systemctl status planharness --no-pager
journalctl -u planharness -n 50 --no-pager

# 本机健康检查
curl -sS http://127.0.0.1:9200/api/v1/health

# 文档页：应输出 /static/swagger/... 而不是 cdn.jsdelivr.net
curl -sS http://127.0.0.1:9200/docs | grep static/swagger

# 公网健康检查
curl -sS https://www.agentworkout.online/api/v1/health

# Postgres 容器端口绑定
docker inspect pg --format '{{json .HostConfig.PortBindings}}'
```

期望：

- `80/443` 绑定 `0.0.0.0`
- `9200/5432` 绑定 `127.0.0.1`
- `/api/v1/health` 返回 `{"code":0,...}`

## 5. 回滚

```bash
sudo systemctl stop planharness
sudo systemctl start <旧服务名>
sudo nginx -t && sudo systemctl reload nginx
```

不要删除旧目录，直到新服务验证通过。

## 6. 2C2G 运行建议

- uvicorn `--workers 1`，不要 2+。
- `--limit-concurrency 20`。
- Postgres `shared_buffers=128MB`、`max_connections=20`。
- 不在服务器上 `npm build`，本地 build 后传 `dist`。
- 监控：

```bash
free -h
docker stats --no-stream
systemctl status planharness
```

- 如果 API 常驻内存超过 500MB，先查 LangGraph/依赖泄漏，不要盲目加 worker。

## 7. 安全清单

- [ ] API 以 `planharness` 用户运行，不是 root
- [ ] `.env` 640 root:planharness，不在 Git
- [ ] 9200/5432 仅 127.0.0.1
- [ ] Nginx 只开 80/443
- [ ] `NoNewPrivileges` / `ProtectSystem` 已开
- [ ] DeepSeek Key 低余额、专用
- [ ] Nginx 限流已开
- [ ] 日志不打印 Authorization/Key
- [ ] `grep -R "sk-" /opt/planharness /etc/planharness` 无结果
