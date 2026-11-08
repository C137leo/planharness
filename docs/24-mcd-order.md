# 麦当劳 MCP：从"推荐"走到"停在付款前"

这一篇只讲一件事：**为什么点餐能做到"订单建好、钱不付"，以及为什么购物车永远是空的。**

## 1. 为什么网页购物车里没有 Agent 的餐食

这是两个互不相通的世界：

| | 麦当劳网页（mcd.cn） | 麦当劳 MCP（mcp.mcd.cn） |
|---|---|---|
| 身份 | 你的手机号登录的浏览器会话 | 一个服务端 Token（本项目配的） |
| 购物车 | 存在浏览器/会员会话里 | **没有"加购物车"这个工具** |
| 下单 | 网页收银台 | `create-order` 直接建单，返回支付链接 |

所以：MCP 查到的、算的价格，**推不进网页购物车**；网页购物车里的东西，MCP 也读不到。
想让"我点的单"出现在你手机上，只有两条路：

1. 用 MCP 直接建单（本项目选的），拿官方支付链接，你自己决定付不付；
2. 自己打开网页/小程序照着点（原来的"去点餐"按钮）。

## 2. 走通的链路（四个工具）

```
query-nearby-stores  城市 + 关键词 → 门店（storeCode）
        ↓
query-meals          门店 → 菜单（115 项，含官方现价，单位元）
        ↓
calculate-price      商品 → 应付金额（单位分）+ 可选取餐方式
        ↓
create-order         建单 → 订单号 + payH5Url（待支付，15 分钟有效）
        ↘ cancel-order → 取消（不影响别的）
```

官方实测（本项目跑过）：

```json
{"order_id":"1030439380000662338747855250","status":"待支付","total":16.0,
 "take_way":"外带","expire_at":"2026-11-05 21:05:20",
 "pay_url":"https://m.mcd.cn/mcp/scanToPay?orderId=1030439380000662338747855250"}
```

`cancel-order` 之后 `query-order` 返回 `已取消`，说明这条链路是闭环的。

## 3. 本项目怎么用

- 后端：`app/food/mcd_order.py`（解析 + 建单/取消）、`app/api/mcd_routes.py`（HTTP 接口）
- 前端：`frontend/src/components/McdOrderSheet.vue`（MCP Token + 城市/门店 → 确认餐品 → 算价 → 建单 → 付款/取消）
- 入口：今日规划结果里麦当劳卡片的「让 Agent 帮我点好（到付款前）→」

菜单匹配规则（`match_items`）：先精确同名，再双向包含；对不上的**不猜**，
单独列成"这家店没有：…"，让用户自己换门店。

接口：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/mcd/status` | 是否已配置、是否允许建单（可用 `X-MCD-Token` 传用户 token） |
| GET | `/api/v1/mcd/stores?city=&keyword=` | 搜门店（官方两个参数都必填） |
| GET | `/api/v1/mcd/menu?store_code=&names=a\|b` | 菜单 + 把推荐餐名对到商品 |
| POST | `/api/v1/mcd/quote` | 官方算价 |
| POST | `/api/v1/mcd/orders` | 建待支付订单 |
| GET | `/api/v1/mcd/orders/{id}` | 查订单状态 |
| POST | `/api/v1/mcd/orders/{id}/cancel` | 取消订单 |

## 3.9 官方还有一条"每日下单数"限制

实测：当天建了 5 单（都取消过）之后，第 6 单返回

```json
{"success": false, "code": 600039, "message": "今日下单数已经上限"}
```

几点说明：

- 这是**麦当劳账号级**的限制（跟 Token 绑定的会员账号），不是我们这个项目的限流，
  文档里也没写具体数值；从"5 单后必定失败"看，当前账号是 **5 单/天**，但换账号/换配置可能不同，
  不要把它当常量写死。
- **已取消的订单也算一天里的下单数**，所以调试时反复建单取消，很容易把自己顶到上限。
- 我们做的处理：`600039` 单独映射成 `mcd_daily_order_limit`（HTTP 429），
  前端提示"今天这个账号的下单次数已经到上限"，并把"生成订单"按钮置灰，
  引导用户用上面的「去麦当劳点餐」自己去点。第二天自然恢复（按自然日算，未单独验证）。

## 4. 护栏（这些都是刻意加的）

- **只做到店自取**（`orderType=1 / beType=1`）：外送要会员地址簿里的 `addressId`，
  我们没有用户的麦当劳账号地址，不猜；到店场景传 `beCode` 会报错，代码里一律不传。
- **60 秒冷却**：同一用户 60 秒内只能建一单，防止双击生成两张待支付单；
  **取消过就不再挡**——冷却防的是连点，不是惩罚用户改主意。
- **写操作开关**：`MCD_MCP_ALLOW_ORDER=0` 可以一键关掉建单/取消，只保留营养库只读。
- **审计链**：每单都会往当前计划的审计流写 `mcd_order_created`（订单号/金额/门店），
  取消写 `mcd_order_cancelled`；Console 里能查到"谁在什么时候点了多少钱"。
- **不碰支付**：不保存支付信息、不代付；`pay_url` 是麦当劳官方 H5 收银台。

## 5. 用户自己的 MCP Token

官方获取地址：<https://open.mcd.cn/mcp>

步骤：手机号登录 → 右上角「控制台」→ 激活 → 复制 MCP Token。
点餐抽屉里有 `MCP Token` 输入框，并带「去麦当劳 MCP 控制台获取 →」链接；请求会带：

```http
X-MCD-Token: <用户输入>
```

后端解析规则（`app/food/mcd_mcp.py::resolve_token`）：

- 空：用服务端 `MCD_MCP_TOKEN`（兼容原来的单账号演示）；
- 与服务端配置的私有口令（`MCD_MCP_TOKEN_ALIAS`）相同：用服务端 `MCD_MCP_TOKEN`；
- 其它值：用用户自己的 token，只随本次请求发往 MCP，不写数据库、不写日志。

前端按登录用户把 token 存在 localStorage 的
`planharness_mcd_token:<user_id>`（user_id 从 JWT 的 `sub` 解出）；
输入框失焦或点「找门店」时保存，同一账号下次打开默认带出。
「今日规划」的麦当劳营养块也会带同一个 token。

默认城市是 **北京**，用户可以直接改；官方 `query-nearby-stores` 只接受
`city + keyword`，没有经纬度入口，所以不做 IP 定位，避免猜错城市。

## 5.1 服务端配置

```dotenv
MCD_MCP_URL=https://mcp.mcd.cn
MCD_MCP_TOKEN=***                 # open.mcd.cn 控制台激活后生成，只放环境文件
MCD_MCP_TOKEN_ALIAS=              # 可选：私有口令；输入它时用上面的 token
MCD_MCP_TOOL=list-nutrition-foods # 营养库（只读）
MCD_MCP_TIMEOUT=15
MCD_MCP_ALLOW_ORDER=1             # 1 允许建单；0 只读
```

`MCD_MCP_TOKEN_ALIAS` 留空时，只认用户自己的 token；配置后，输入该口令会使用
服务端 token。真实值不要提交 Git，也不要写进前端。

dev 写 `backend/.env`，prod 写 `/etc/planharness/planharness.env`
（systemd `EnvironmentFile`），改完 `systemctl restart planharness`。

## 6. 已知边界 / 没做的事

- **一个 Token 一个账号**：默认所有用户共用服务端 MCP Token；现在点餐抽屉支持用户填自己的
  token（配置了 `MCD_MCP_TOKEN_ALIAS` 时，输入该口令映射到服务端 token），
  订单落在该 token 对应的账号下。
  不填时仍走服务端 token。
- **外送没做**：要接 `delivery-query-addresses` → 选地址 → `orderType=2`，
  而且地址是 Token 主人账号里的，这个演示不合适。
- **没有购物车语义**：建单即订单，没有"先放购物车再改"的中间态；
  想改就是取消重下（官方 `cancel-order` 支持）。
- **未验证**：真机支付页跳转、弱网/超时下的重试、多个用户同时建单的并发。
- **会真的产生订单**：只在你点"生成订单"时发生，且 15 分钟不付就失效、也能即时取消。
- **自动定位已去掉**：`query-nearby-stores` 只接受 `city` + `keyword`，没有经纬度入口；
  默认北京，用户自己改（会记住上次的城市/关键词）。
- **每天能下几单不是我们能定的**：见 3.9，官方账号级限制，文档没写数值，实测 5 单后报
  `600039 今日下单数已经上限`。
