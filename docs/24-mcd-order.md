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
- 前端：`frontend/src/components/McdOrderSheet.vue`（选门店 → 确认餐品 → 算价 → 建单 → 付款/取消）
- 入口：今日规划结果里麦当劳卡片的「让 Agent 帮我点好（到付款前）→」

菜单匹配规则（`match_items`）：先精确同名，再双向包含；对不上的**不猜**，
单独列成"这家店没有：…"，让用户自己换门店。

接口：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/mcd/status` | 是否已配置、是否允许建单 |
| GET | `/api/v1/mcd/stores?city=&keyword=` | 搜门店（官方两个参数都必填） |
| GET | `/api/v1/mcd/menu?store_code=&names=a\|b` | 菜单 + 把推荐餐名对到商品 |
| POST | `/api/v1/mcd/quote` | 官方算价 |
| POST | `/api/v1/mcd/orders` | 建待支付订单 |
| GET | `/api/v1/mcd/orders/{id}` | 查订单状态 |
| POST | `/api/v1/mcd/orders/{id}/cancel` | 取消订单 |

## 3.1 默认位置：按 IP 猜城市

麦当劳的 `query-nearby-stores` **只接受 `city` + `keyword` 两个文本参数，没有经纬度入口**，
所以"定位"对它来说就是"给个城市名"。做法：

1. `GET /api/v1/mcd/locate` 取真实客户端 IP（`X-Forwarded-For` 第一段）；
2. 公网 IP → 查 ip-api.com（免费、中文）；本机/内网（127.0.0.1）→ 退回**服务器出口 IP**，
   这样本地开发也能得到真实城市；
3. 城市名去掉"市/地区/州"后缀（`济南市` → `济南`，带后缀容易搜偏）；
4. 前端打开点餐抽屉就自动用这个城市当 `keyword` 搜一次，按距离排序列出最近的几家店；
5. 页面上写明来源（"按你的 IP" / "按服务器出口 IP"），随时可以手动改城市和关键词。

IP 只发给 ip-api.com 做一次查询（结果缓存 1 小时），日志和界面里只展示打码后的 IP
（IPv4 抹最后一段）。定位失败不影响手动选门店。

## 4. 护栏（这些都是刻意加的）

- **只做到店自取**（`orderType=1 / beType=1`）：外送要会员地址簿里的 `addressId`，
  我们没有用户的麦当劳账号地址，不猜；到店场景传 `beCode` 会报错，代码里一律不传。
- **60 秒冷却**：同一用户 60 秒内只能建一单，防止双击生成两张待支付单；
  **取消过就不再挡**——冷却防的是连点，不是惩罚用户改主意。
- **写操作开关**：`MCD_MCP_ALLOW_ORDER=0` 可以一键关掉建单/取消，只保留营养库只读。
- **审计链**：每单都会往当前计划的审计流写 `mcd_order_created`（订单号/金额/门店），
  取消写 `mcd_order_cancelled`；Console 里能查到"谁在什么时候点了多少钱"。
- **不碰支付**：不保存支付信息、不代付；`pay_url` 是麦当劳官方 H5 收银台。

## 5. 配置

```dotenv
MCD_MCP_URL=https://mcp.mcd.cn
MCD_MCP_TOKEN=***                 # open.mcd.cn 控制台激活后生成
MCD_MCP_TOOL=list-nutrition-foods # 营养库（只读）
MCD_MCP_TIMEOUT=15
MCD_MCP_ALLOW_ORDER=1             # 1 允许建单；0 只读
```

dev 写 `backend/.env`，prod 写 `/etc/planharness/planharness.env`
（systemd `EnvironmentFile`），改完 `systemctl restart planharness`。

## 6. 已知边界 / 没做的事

- **一个 Token 一个账号**：所有用户共用这套 MCP Token，订单都会落在 Token 主人的会员账号下。
  要做多用户，就得让每个用户绑自己的 MCP Token（控制台里各自激活）。
- **外送没做**：要接 `delivery-query-addresses` → 选地址 → `orderType=2`，
  而且地址是 Token 主人账号里的，这个演示不合适。
- **没有购物车语义**：建单即订单，没有"先放购物车再改"的中间态；
  想改就是取消重下（官方 `cancel-order` 支持）。
- **未验证**：真机支付页跳转、弱网/超时下的重试、多个用户同时建单的并发。
- **会真的产生订单**：只在你点"生成订单"时发生，且 15 分钟不付就失效、也能即时取消。
