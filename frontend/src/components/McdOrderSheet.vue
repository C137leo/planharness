<script setup lang="ts">
/**
 * 麦当劳点餐抽屉：找门店 → 确认餐品 → 算价 → 建单（停在付款前）。
 *
 * 这一步是"真的在麦当劳侧建一张待支付订单"，返回官方 H5 收银台链接，
 * 用户自己决定付不付；不付就过期（15 分钟），也能在这里一键取消。
 *
 * 为什么不是"加进网页购物车"：网页购物车是浏览器会话，MCP 是另一条通道，
 * 两边不互通；官方 MCP 也没有"加购物车"工具，只有 create-order。
 */
import { computed, ref, watch } from 'vue'
import { api, ApiError, getStoredMcdToken, setStoredMcdToken, type McdMenuItem, type McdOrder, type McdQuote, type McdStore } from '../api'

const props = defineProps<{
  visible: boolean
  /** 推荐里的餐名（营养库叫法），用来匹配这家店的真实商品 */
  suggestions: string[]
}>()
const emit = defineEmits<{ (e: 'close'): void }>()

const step = ref<'store' | 'items' | 'order'>('store')
// 没接定位：默认北京，用户自己改；上次搜过的城市/关键词记一下。
const LAST_CITY_KEY = 'planharness_mcd_city'
const LAST_KEYWORD_KEY = 'planharness_mcd_keyword'
const city = ref((uni.getStorageSync(LAST_CITY_KEY) as string) || '北京')
const keyword = ref((uni.getStorageSync(LAST_KEYWORD_KEY) as string) || '')
// 用户自己的 MCD token；是否映射服务端 token 由后端配置决定
const mcdToken = ref(getStoredMcdToken())

function persistMcdToken() {
  setStoredMcdToken(mcdToken.value.trim())
}
const stores = ref<McdStore[]>([])
const store = ref<McdStore | null>(null)
const matched = ref<Array<McdMenuItem & { suggestion: string }>>([])
const missing = ref<string[]>([])
const qty = ref<Record<string, number>>({})
const takeWay = ref<Array<{ code: string; title: string; subtitle?: string }>>([])
const takeWayCode = ref('')
const quote = ref<McdQuote | null>(null)
const order = ref<McdOrder | null>(null)
const loading = ref(false)
const creating = ref(false)
const error = ref('')
/** 官方账号级"今日下单数上限"：命中后当天不再让人点，免得白等 */
const limitHit = ref(false)

const selected = computed(() =>
  Object.entries(qty.value)
    .filter(([, count]) => count > 0)
    .map(([code, count]) => ({ product_code: code, quantity: count })),
)
const selectedTotal = computed(() =>
  matched.value.reduce((sum, item) => sum + item.price * (qty.value[item.product_code] || 0), 0),
)

function reset() {
  step.value = 'store'
  stores.value = []
  store.value = null
  matched.value = []
  missing.value = []
  qty.value = {}
  takeWay.value = []
  takeWayCode.value = ''
  quote.value = null
  order.value = null
  error.value = ''
}

watch(
  () => props.visible,
  (open) => {
    if (!open) return
    mcdToken.value = getStoredMcdToken()
    reset()
  },
)

function fail(err: unknown, fallback: string) {
  error.value = err instanceof ApiError ? err.message : fallback
}

async function searchStores(auto = false) {
  if (!keyword.value.trim()) {
    if (!auto) error.value = '填个门店或地标关键词，例如 人民广场'
    return
  }
  loading.value = true
  if (!auto) error.value = ''
  try {
    setStoredMcdToken(mcdToken.value.trim())
    const data = await api.mcdStores(
      city.value.trim() || '北京',
      keyword.value.trim(),
      mcdToken.value.trim(),
    )
    uni.setStorageSync(LAST_CITY_KEY, city.value.trim() || '北京')
    uni.setStorageSync(LAST_KEYWORD_KEY, keyword.value.trim())
    // 官方已按距离排过，这里再稳一次：近的在前
    stores.value = [...data.stores].sort(
      (a, b) => (a.distance_m ?? 1e9) - (b.distance_m ?? 1e9),
    )
    if (auto) error.value = ''
  } catch (err) {
    fail(err, '搜门店失败')
  } finally {
    loading.value = false
  }
}

async function pickStore(target: McdStore) {
  loading.value = true
  error.value = ''
  try {
    const menu = await api.mcdMenu(target.store_code, props.suggestions, mcdToken.value.trim())
    store.value = target
    matched.value = menu.matched
    missing.value = menu.missing
    qty.value = Object.fromEntries(menu.matched.map((item) => [item.product_code, 1]))
    // 推荐餐品都匹配不上时不硬点：让用户回到列表换一家店
    step.value = menu.matched.length ? 'items' : 'items'
    if (!menu.matched.length) error.value = '这家店的菜单里没找到推荐的餐品，换一家试试'
    quote.value = null
  } catch (err) {
    fail(err, '菜单加载失败')
  } finally {
    loading.value = false
  }
}

function changeQty(code: string, delta: number) {
  const next = Math.max(0, Math.min(9, (qty.value[code] || 0) + delta))
  qty.value = { ...qty.value, [code]: next }
  quote.value = null
}

async function getQuote() {
  if (!store.value || !selected.value.length) {
    error.value = '至少选一样再算价'
    return
  }
  loading.value = true
  error.value = ''
  try {
    quote.value = await api.mcdQuote(
      store.value.store_code, selected.value, mcdToken.value.trim())
    takeWay.value = quote.value.take_way
    if (!takeWayCode.value && quote.value.take_way.length) {
      takeWayCode.value = quote.value.take_way[0].code
    }
  } catch (err) {
    fail(err, '算价失败')
  } finally {
    loading.value = false
  }
}

async function createOrder() {
  if (!store.value || !selected.value.length || creating.value) return
  creating.value = true
  error.value = ''
  try {
    order.value = await api.mcdCreateOrder(
      store.value.store_code,
      selected.value,
      takeWayCode.value,
      mcdToken.value.trim(),
    )
    step.value = 'order'
  } catch (err) {
    if (err instanceof ApiError && err.statusCode === 429) limitHit.value = true
    fail(err, '建单失败')
  } finally {
    creating.value = false
  }
}

async function cancelOrder() {
  if (!order.value) return
  loading.value = true
  try {
    await api.mcdCancelOrder(order.value.order_id, mcdToken.value.trim())
    uni.showToast({ title: '订单已取消', icon: 'none' })
    order.value = null
    step.value = 'items'
  } catch (err) {
    fail(err, '取消失败，可以在麦当劳 App 里取消')
  } finally {
    loading.value = false
  }
}

function openPay() {
  const url = order.value?.pay_url
  if (!url) return
  const globalOpen = (globalThis as unknown as { open?: (u: string, t?: string) => unknown }).open
  if (typeof globalOpen === 'function') {
    globalOpen(url, '_blank')
    return
  }
  uni.setClipboardData({
    data: url,
    success: () => uni.showToast({ title: '收银台链接已复制', icon: 'none' }),
  })
}

function copy(text: string) {
  uni.setClipboardData({
    data: text,
    success: () => uni.showToast({ title: '已复制', icon: 'none' }),
  })
}
</script>

<template>
  <view v-if="visible">
    <view class="sheet-mask mcd-order-mask" @click="emit('close')" />
    <view class="sheet mcd-order-sheet">
      <view class="row">
        <view class="section-title">麦当劳 · 帮你点好（到付款前）</view>
        <text class="link" @click="emit('close')">关闭</text>
      </view>

      <view v-if="error" class="error" style="margin-top: 10px">{{ error }}</view>

      <!-- 1. 选门店 -->
      <view v-if="step === 'store'">
        <text class="mcd-order-tip">
          官方 MCP 没有"加购物车"这个动作，但可以在麦当劳侧直接建一张待支付订单：
          选好门店和餐品，我把订单建好，付款由你自己点。
        </text>
        <view class="mcd-field">
          <text class="mcd-label">MCP Token</text>
          <input
            v-model="mcdToken"
            class="input"
            placeholder="输入你的 MCD Token（选填）"
            @blur="persistMcdToken"
          />
        </view>
        <view class="mcd-field">
          <text class="mcd-label">城市</text>
          <input v-model="city" class="input" placeholder="北京" />
        </view>
        <view class="mcd-field">
          <text class="mcd-label">门店 / 地标</text>
          <input v-model="keyword" class="input" placeholder="例如 国贸 / 三里屯" @confirm="searchStores" />
        </view>
        <view class="btn-row">
          <view class="btn-sm primary" @click="searchStores">
            {{ loading ? '搜索中…' : '找门店' }}
          </view>
        </view>

        <view v-if="stores.length" class="mcd-store-list">
          <view v-for="item in stores" :key="item.store_code" class="mcd-store" @click="pickStore(item)">
            <view class="mcd-store-main">
              <text class="mcd-store-name">{{ item.name }}</text>
              <text class="list-meta">
                <text v-if="item.distance_m != null">{{ item.distance_m }} m · </text>{{ item.hours }}
                <text v-if="!item.open"> · 休息中</text>
              </text>
              <text class="mcd-store-address">{{ item.address }}</text>
            </view>
            <text class="mcd-store-go">选这家 →</text>
          </view>
        </view>
      </view>

      <!-- 2. 确认餐品 + 算价 -->
      <view v-else-if="step === 'items'">
        <view class="mcd-picked-store">
          <text class="list-title">{{ store?.name }}</text>
          <text class="list-meta">{{ store?.address }}</text>
        </view>

        <view v-for="item in matched" :key="item.product_code" class="mcd-pick">
          <view class="mcd-pick-main">
            <text class="list-title">{{ item.name }}</text>
            <text class="list-meta">推荐里叫「{{ item.suggestion }}」 · 单份 ¥{{ item.price.toFixed(1) }}</text>
          </view>
          <view class="mcd-stepper">
            <view class="mcd-step-btn" @click="changeQty(item.product_code, -1)">−</view>
            <text class="mcd-qty">{{ qty[item.product_code] || 0 }}</text>
            <view class="mcd-step-btn" @click="changeQty(item.product_code, 1)">+</view>
          </view>
        </view>

        <text v-if="missing.length" class="mcd-missing">
          这家店没有：{{ missing.join('、') }}（换一家门店或自己再看一眼菜单）
        </text>

        <view v-if="takeWay.length" class="mcd-takeway">
          <text class="mcd-label">取餐方式</text>
          <view class="mcd-takeway-row">
            <view
              v-for="way in takeWay"
              :key="way.code"
              class="pill"
              :class="{ 'pill-on': takeWayCode === way.code }"
              @click="takeWayCode = way.code"
            >
              {{ way.title }}
            </view>
          </view>
        </view>

        <view class="mcd-total">
          <text class="mcd-total-label">{{ selected.length }} 样 · 预计</text>
          <text class="mcd-total-value">¥{{ selectedTotal.toFixed(1) }}</text>
        </view>
        <text v-if="quote" class="mcd-meta">
          官方算价 ¥{{ quote.price }}<text v-if="quote.discount && quote.discount !== '0.00'">（已优惠 ¥{{ quote.discount }}）</text>
        </text>

        <view class="btn-row">
          <view class="btn-sm" @click="step = 'store'">换门店</view>
          <view class="btn-sm" @click="getQuote">{{ loading ? '算价中…' : '官方算价' }}</view>
          <view v-if="limitHit" class="btn-sm" style="opacity: 0.6">
            今日下单已达上限
          </view>
          <view v-else class="btn-sm primary" @click="createOrder">
            {{ creating ? '建单中…' : '生成订单（到付款前）' }}
          </view>
        </view>
        <text class="mcd-meta">
          注意：点这一下会真的在麦当劳生成一张待支付订单（15 分钟内不付会自动失效），
          我们不会替你付款、也不收钱。
        </text>
      </view>

      <!-- 3. 订单已生成 -->
      <view v-else-if="step === 'order' && order">
        <view class="mcd-order-card">
          <view class="row">
            <text class="mcd-order-title">订单已生成 · {{ order.status }}</text>
            <text class="mcd-order-total">¥{{ order.total.toFixed(2) }}</text>
          </view>
          <text class="list-meta">{{ order.store_name }}<text v-if="order.take_way"> · {{ order.take_way }}</text></text>
          <view v-for="item in order.items" :key="item.name" class="row" style="margin-top: 6px">
            <text class="list-meta">{{ item.name }} × {{ item.quantity }}</text>
            <text class="list-meta">¥{{ item.price.toFixed(1) }}</text>
          </view>
          <text class="mcd-meta">订单号 {{ order.order_id }} · 请在 {{ order.expire_at }} 前完成支付</text>
        </view>

        <view class="btn-row">
          <view class="btn-sm primary" @click="openPay">去麦当劳付款 →</view>
          <view class="btn-sm" @click="copy(order.order_id)">复制订单号</view>
          <view class="btn-sm" @click="cancelOrder">取消订单</view>
        </view>
        <text class="mcd-meta">
          付款页面在麦当劳自己的站点上；不想买就点"取消订单"，或者什么都不做等它过期。
        </text>
      </view>
    </view>
  </view>
</template>

<style>
/* 这是叠在"今日规划"结果抽屉上的第二层：背景压暗一点、卡片带投影，
   否则两张长一个样，看起来像是同一张卡片被切开了。 */
.mcd-order-mask {
  background: rgba(43, 26, 18, 0.58);
  z-index: 1300;
}

.mcd-order-sheet {
  box-shadow: 0 24px 60px rgba(43, 26, 18, 0.3);
  z-index: 1301;
}

.mcd-order-tip {
  color: var(--muted);
  display: block;
  font-size: 12px;
  line-height: 1.55;
  margin-top: 8px;
}

.mcd-field {
  margin-top: 10px;
}

.mcd-label {
  color: var(--muted);
  font-size: 11px;
}

.mcd-store-list {
  margin-top: 10px;
}

.mcd-store {
  align-items: center;
  border-top: 1px solid var(--line-soft);
  display: flex;
  gap: 10px;
  justify-content: space-between;
  padding: 11px 0;
}

.mcd-store-main {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}

.mcd-store-name {
  color: var(--ink);
  font-size: 14px;
  font-weight: 600;
  line-height: 1.35;
}

.mcd-store-address {
  color: var(--muted);
  font-size: 11px;
  line-height: 1.4;
}

.mcd-store-go {
  color: #c2410c;
  flex: 0 0 auto;
  font-size: 12px;
  font-weight: 700;
  white-space: nowrap;
}

.mcd-picked-store {
  background: #fff5f1;
  border-radius: 12px;
  display: flex;
  flex-direction: column;
  gap: 3px;
  margin-top: 12px;
  padding: 10px 12px;
}

.mcd-pick {
  align-items: center;
  border-top: 1px solid var(--line-soft);
  display: flex;
  gap: 10px;
  justify-content: space-between;
  padding: 11px 0;
}

.mcd-pick-main {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}

.mcd-stepper {
  align-items: center;
  display: flex;
  flex: 0 0 auto;
  gap: 8px;
}

.mcd-step-btn {
  align-items: center;
  background: var(--primary-soft);
  border-radius: 999px;
  color: var(--primary-deep);
  display: flex;
  font-size: 16px;
  font-weight: 700;
  height: 26px;
  justify-content: center;
  width: 26px;
}

.mcd-qty {
  color: var(--ink);
  font-size: 14px;
  font-variant-numeric: tabular-nums;
  min-width: 14px;
  text-align: center;
}

.mcd-missing {
  color: var(--accent);
  display: block;
  font-size: 12px;
  line-height: 1.5;
  margin-top: 8px;
}

.mcd-takeway {
  margin-top: 12px;
}

.mcd-takeway-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 6px;
}

.pill-on {
  background: var(--primary);
  color: #fff;
}

.mcd-total {
  align-items: baseline;
  display: flex;
  gap: 8px;
  justify-content: space-between;
  margin-top: 14px;
}

.mcd-total-label {
  color: var(--muted);
  font-size: 12px;
}

.mcd-total-value {
  color: var(--primary-deep);
  font-size: 20px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}

.mcd-meta {
  color: var(--muted);
  display: block;
  font-size: 11px;
  line-height: 1.5;
  margin-top: 6px;
}

.mcd-order-card {
  background: #fff5f1;
  border-radius: 14px;
  margin-top: 12px;
  padding: 12px 14px;
}

.mcd-order-title {
  color: #c2410c;
  font-size: 14px;
  font-weight: 700;
}

.mcd-order-total {
  color: var(--ink);
  font-size: 18px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
</style>
