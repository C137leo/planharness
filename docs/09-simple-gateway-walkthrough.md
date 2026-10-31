# 09 最简单网关：把 DeepSeek Key 从服务器拿走

## 1. 先用一句话解释

网关不是“再买一台服务器”，而是一个**只做转发的云函数**：

```text
你的服务器  --(GATEWAY_TOKEN)-->  云函数  --(DEEPSEEK_API_KEY)-->  DeepSeek
```

- 云函数里放真正的 DeepSeek Key。
- 你的服务器只放一个“门禁卡” `GATEWAY_TOKEN`。
- 服务器被入侵，攻击者拿到的是门禁卡，不是 DeepSeek Key；门禁卡可以立刻换掉。

## 2. 只有一台服务器，能做到吗？

**不接任何外部服务：做不到。**

只要服务器要调用 DeepSeek，Key 就必须在这台服务器上；root 被攻破，Key 就能被读走。加密也只能防“磁盘被拿走”，防不了“服务器运行时被控制”。

所以只有两条路：

1. **加一个云函数**（Cloudflare Worker / 腾讯云 SCF），Key 不放服务器。
2. **接受风险**：Key 放服务器，但用低余额、独立 Key、权限、日志脱敏、轮换来降低损失。

## 3. 推荐：Cloudflare Worker

不需要迁移域名，不需要第二台服务器；用 `*.workers.dev` 地址即可。

### 3.1 创建

1. 注册/登录 Cloudflare。
2. Workers & Pages → Create → Worker。
3. 命名，例如 `planharness-ai`。
4. Edit code，粘贴下面的最小实现。
5. Settings → Variables and Secrets：
   - `DEEPSEEK_API_KEY`：真正的 DeepSeek Key（Secret）
   - `GATEWAY_TOKEN`：随机 32 字节以上（Secret）
   - `ALLOWED_IP`：`120.53.242.254`（你的服务器 IP）
6. Deploy，得到 `https://planharness-ai.<你的子域>.workers.dev`。

### 3.2 最小 Worker 代码

```js
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method !== "POST" || url.pathname !== "/v1/chat/completions") {
      return new Response("Not Found", { status: 404 });
    }

    const auth = request.headers.get("Authorization") || "";
    if (auth !== `Bearer ${env.GATEWAY_TOKEN}`) {
      return new Response("Unauthorized", { status: 401 });
    }

    const ip = request.headers.get("CF-Connecting-IP");
    if (env.ALLOWED_IP && ip !== env.ALLOWED_IP) {
      return new Response("Forbidden", { status: 403 });
    }

    let body;
    try {
      body = await request.json();
    } catch {
      return new Response("Bad JSON", { status: 400 });
    }

    if (body.model !== "deepseek-chat") {
      return new Response("Model not allowed", { status: 400 });
    }
    body.max_tokens = Math.min(Number(body.max_tokens) || 2000, 2000);

    const upstream = await fetch("https://api.deepseek.com/v1/chat/completions", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${env.DEEPSEEK_API_KEY}`,
      },
      body: JSON.stringify(body),
    });

    return new Response(upstream.body, {
      status: upstream.status,
      headers: { "Content-Type": "application/json" },
    });
  },
};
```

> 这是最小版本：只做 token、IP、模型、max_tokens。  
> 生产再加限流/预算/日志；MVP 先用 Cloudflare 的 Rate Limiting 规则或 KV。

### 3.3 服务器配置

```text
DEEPSEEK_BASE_URL=https://planharness-ai.<你的子域>.workers.dev/v1
DEEPSEEK_API_KEY=<GATEWAY_TOKEN>
DEEPSEEK_MODEL=deepseek-chat
```

这里的 `DEEPSEEK_API_KEY` 是门禁卡，不是 DeepSeek Key。  
代码仍是 OpenAI 兼容接口，不用改。

### 3.4 验证

```bash
# 1. 正确 token -> 200
curl -sS https://xxx.workers.dev/v1/chat/completions \
  -H "Authorization: Bearer $GATEWAY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"hi"}],"max_tokens":16}'

# 2. 错 token -> 401
curl -i ... -H "Authorization: Bearer wrong"

# 3. 非服务器 IP -> 403（用手机流量测）
```

## 4. 腾讯云 SCF 备选

如果 `workers.dev` 在国内服务器访问不稳定：

- 腾讯云 SCF → 创建函数 → 环境变量放 `DEEPSEEK_API_KEY`。
- 函数 URL 做 token 校验 + IP 白名单 + max_tokens。
- 应用服务器调用函数 URL。
- 逻辑和 Worker 一样，只是换平台。

## 5. 如果暂时不做网关

先按“低损失”策略跑起来：

1. DeepSeek 后台新建**专用 Key**，余额只放几十元。
2. 服务器 `/etc/planharness/planharness.env`，`chmod 600`，owner 为应用用户。
3. 不提交 Git、不放前端、不打印日志、不放 Docker 镜像。
4. 服务器加固：SSH 密钥、UFW 只开 22/80/443、fail2ban、数据库不暴露。
5. 公开演示前把 Key 换掉，或迁到网关。

## 6. 总结

- 只有一台服务器 + 不接外部服务：Key 必然在服务器上，无法绝对安全。
- 想“服务器没有 Key”：用 Cloudflare Worker / 腾讯云 SCF。
- 想“Key 被偷了也损失有限”：独立 Key + 低余额 + 轮换 + 限流。
- 想“别人不能白用你的模型”：应用层登录/邀请码/限流/每日预算，和网关是两件事。
