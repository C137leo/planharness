# 审计哈希链：作用是什么、怎么设计的、防不住什么

## 0. 一句话

**哈希链不是为了"记日志"，而是为了在事后能证明"这份记录没被悄悄改过"。**
每条事件的 hash 把上一条的 hash 算进去，于是任何一处改动都会让后面整条链对不上。

## 1. 它具体解决三个问题

| 场景 | 没有链 | 有链 |
|---|---|---|
| 有人偷偷改了补丁的 payload | 看不出来 | `payload_hash` 对不上，`verify` 直接报 `broken_at` |
| 有人删掉中间一条审批记录 | 看不出来 | `seq` 断档 + 下一条 `prev_hash` 对不上 |
| 事后要回答"这版是谁批的" | 只能相信日志 | 链 + 审批记录 + 版本，三者可以互相对上 |

对应到本项目的事件：`patch_created / approval_granted / approval_rejected /
patch_committed / rollback_committed / plan_confirmed / mcd_order_created …`

## 2. 设计（`backend/app/audit.py`）

### 2.1 数据结构

```python
class AuditEvent(Base):
    stream_id: str          # 一条链 = 一个计划（plan_id）；没计划时挂 user_id
    seq: int                # 链内序号，从 1 开始，(stream_id, seq) 唯一
    action: str             # 受控枚举（ACTIONS）
    actor_type: str         # user / agent / system
    actor_id: str
    payload: dict           # 业务内容（补丁 id、版本号、订单号…）
    payload_hash: str       # sha256(canonical_payload(payload))
    prev_hash: str          # 上一条的 hash；第一条是 GENESIS_HASH（64 个 0）
    hash: str               # 本条事件的 hash
    created_at: datetime
```

### 2.2 两个 hash

```python
def canonical_payload(payload):
    # 键排序 + 紧凑分隔符：{"a":1,"b":2} 与 {"b":2,"a":1} 必须得到同一串
    return json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

def event_hash(stream_id, seq, action, actor_type, actor_id, payload_digest, prev_hash):
    material = "|".join([stream_id, str(seq), action, actor_type, actor_id,
                         payload_digest, prev_hash])
    return sha256(material)
```

为什么要单独有 `payload_hash`：校验时先比 payload 摘要，能区分"payload 被改"和"链被替换"，
错误信息更好定位（`payload_hash mismatch` vs `prev_hash mismatch`）。

### 2.3 追加流程

```python
previous = last_event(db, stream_id)
seq = previous.seq + 1 if previous else 1
prev_hash = previous.hash if previous else GENESIS_HASH
digest = payload_hash(payload)
event = AuditEvent(..., payload_hash=digest, prev_hash=prev_hash,
                   hash=event_hash(..., payload_digest=digest, prev_hash=prev_hash))
db.add(event); db.flush()          # 不 commit：跟业务写在同一事务里
```

**不 commit 是故意的**：审计事件和业务变更要么一起成功、要么一起回滚，
不会出现"计划改了但审计没记"或"审计记了但计划没改"。

### 2.4 校验流程（`verify_stream`）

按 `seq asc` 从头重算，四步依次检查：

1. `seq == index`（连续）→ 抓"删中间一条"
2. `payload_hash(payload) == event.payload_hash` → 抓"改内容"
3. `event.prev_hash == expected_prev` → 抓"替换/删除前一条"
4. 重算 `event.hash` 与自己比 → 抓"改了内容还硬保留旧 hash"

任何一步失败就返回 `broken_at`（第几条）和 `reason`，Console 直接显示"断裂 @ #N"。

## 3. 它防不住什么（重要，面试要主动讲）

纯链内校验有两个盲区：

| 攻击 | 链内能发现吗 | 补法 |
|---|---|---|
| 改中间一条 | ✅ | — |
| 删中间一条 | ✅（seq 断档） | — |
| **重算整条链**（有库写权限） | ❌ 看起来完全合法 | 需要**外部锚点** |
| **删掉尾部 N 条** | ❌ 前缀本来就是合法链 | 需要**外部锚点** |

所以这次加了「链头签名」：

```python
def sign_head(secret, stream_id, seq, head_hash):
    return HMAC-SHA256(secret, f"{stream_id}|{seq}|{head_hash}")
```

`GET /audit/{stream_id}/verify` 现在返回：

```json
{"valid": true, "count": 120, "head_seq": 120, "head_hash": "3a65…",
 "signature": "68ce…", "signature_alg": "HMAC-SHA256(SECRET_KEY)"}
```

把 `(stream_id, head_seq, head_hash, signature)` 抄到**数据库之外**（工单、邮件、另一台机器、
甚至贴在 release note 里），之后：

- 有人重算整条链 → 链头变了 → 签名对不上；
- 有人删掉尾部 → 链头变短 → 签名对不上。

### 还没做到的（诚实边界）

- **HMAC 是共享密钥**：有服务端 `SECRET_KEY` 的人能重新签名。真正"不可抵赖"要用**非对称签名
  （Ed25519）+ 外部时间戳/WORM 存储**，让签名只可能由持有私钥的一方产生；
- 现在**没有自动锚定**：签名要人手动抄走，抄走之前被重写仍然发现不了；
- `verify` 是 O(N) 全量重算，大 stream 需要分段或 Merkle 化。

## 4. Console 里怎么体现

`AUDIT CHAIN` 卡：

- 事件列表：`#1 plan_confirmed / user/…` + `0000… → c3de…`（prev → hash）
- 按钮「验证链完整性」→ `✓ 120 条事件逐条校验通过`
- 新增：**链头 `#120 · 3a65…` + 签名 `68ce…` + 「复制校验点」**
  文案直接写明"抄到库外，才能发现整链重写/尾部截断"

## 5. 面试怎么讲（30 秒）

> 审计我做的不是普通日志，是哈希链：每条事件的 hash 把上一条的 hash 算进去，
> 所以改中间任何一条，后面全对不上，`verify` 能定位到第几条断的。
> 但纯链只能防中间篡改，防不住"整链重算"和"尾部截断"——所以我给链头加了
> HMAC 签名，把 `(seq, hash, signature)` 抄到库外当锚点，链被动过签名就变。
> 严格的不可抵赖还需要非对称签名 + 外部时间戳，这块我留在文档里没装。

**追问：为什么不直接用数据库审计表？**
A：表只能证明"现在是这样"，证明不了"没被改过"。链 + 签名才是证据。

**追问：为什么不用区块链？**
A：这里只有一个写入方（本服务），不需要共识；哈希链 + 锚点已经能覆盖威胁模型，
区块链是过度设计（运维成本、延迟）。

**追问：怎么防止运维改库？**
A：链内防不住，靠锚点；再往上就是 WORM 存储/独立审计服务，那是安全团队的活。

**追问：性能？**
A：写入是 O(1)（只读上一条）；校验 O(N)，目前按 plan 分链、单链百级，够用。

## 6. 代码与测试

| 位置 | 内容 |
|---|---|
| `backend/app/audit.py` | `canonical_payload` / `payload_hash` / `event_hash` / `append_event` / `verify_stream` / `sign_head` |
| `backend/app/api/routes.py` | `GET /audit/{id}`、`GET /audit/{id}/verify`（带链头签名） |
| `backend/tests/test_audit.py` | 9 个用例：正常流程、改 payload、重算 hash、删中间、签名随链头变化、越权 404 |
