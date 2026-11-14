<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { api, getStoredMcdToken, setStoredMcdToken, type AgentActionChip, type AgentChatResult, type CommerceAction } from '../api'
import { paymentPresentation } from '../styles/commerceUi'
import { selectedCart } from '../styles/commerceCart'
import StableScroll from './StableScroll.vue'
const props = defineProps<{ result: AgentChatResult | null; actions: AgentActionChip[]; busy: boolean }>()
const emit = defineEmits<{
  (e: 'action', action: AgentActionChip): void
  (e: 'commerce', action: CommerceAction): void
  (e: 'ask', text: string): void
  (e: 'open', url: string): void
}>()
const guide = 'https://open.mcd.cn/mcp'
const tokenOpen = ref(false), token = ref(''), localError = ref('')
const connection = ref('loading'), tab = ref<'order' | 'nutrition'>('order')
const city = ref(''), keyword = ref(''), search = ref(''), quantities = ref<Record<string,number>>({})
const menuOpen = ref(false), locationOpen = ref(false), locationSaving = ref(false)
const savedLocation = ref({city:'',keyword:''}), locationNote = ref('')
let locationEdited = false
const now = ref(Date.now())
let timer: ReturnType<typeof setInterval> | undefined
let connectionRevision = 0
let tokenPanelChosen = false
onMounted(async () => {
  timer = setInterval(() => { now.value = Date.now() }, 15000)
  api.commercePreferences().then(value=>{
    savedLocation.value=value
    if (!locationEdited && !city.value && !keyword.value) { city.value=value.city;keyword.value=value.keyword }
  }).catch(()=> { locationNote.value='默认位置暂时未能加载，可手动填写。' })
  const revision = connectionRevision
  try {
    const configured=(await api.commerceStatus()).status
    if (revision !== connectionRevision) return
    connection.value = configured
    if (!tokenPanelChosen) tokenOpen.value = configured === 'token_required'
  } catch {
    if (revision !== connectionRevision) return
    connection.value = 'unknown'
    if (!tokenPanelChosen) tokenOpen.value = false
    localError.value = '暂时无法检查 Token 状态，可点击“Token 设置”查看或更换。'
  }
})
onUnmounted(() => { if (timer) clearInterval(timer) })
watch(() => props.result?.commerce_connection, value => {
  if (!value) return
  connectionRevision++
  connection.value = value.status
  if (value.status === 'token_required') { tokenPanelChosen = false; tokenOpen.value = true }
  else if (!tokenPanelChosen) tokenOpen.value = false
})
const checkout = computed(() => props.result?.checkout)
const catalog = computed(() => props.result?.commerce_catalog)
const nutrition = computed(() => props.result?.commerce_nutrition)
const order = computed(() => props.result?.order || checkout.value?.order)
const conflict = computed(() => props.result?.order_conflict)
const conflictAction = (type:string,id?:string) => props.actions.filter(a=>a.type===type && (!id || a.existing_checkout_id===id))
watch(() => catalog.value?.store_id, () => { quantities.value = {}; search.value = '' })
watch(catalog,value=>{
  const location=value?.items.find(x=>x.search)?.search
  if (location) { city.value=location.city;keyword.value=location.keyword }
},{immediate:true})
const isNutrition = computed(() => tab.value === 'nutrition')
watch(() => props.result, value => {
  if (value?.checkout || value?.commerce_catalog) tab.value='order'
  else if (value?.commerce_nutrition) tab.value='nutrition'
}, { immediate:true })
watch(() => [props.result?.commerce_view,checkout.value?.id,checkout.value?.version], () => {
  menuOpen.value=props.result?.commerce_view==='edit_menu'
  if (menuOpen.value && checkout.value) quantities.value=Object.fromEntries(checkout.value.cart.map(item=>[item.product_id,item.quantity]))
},{immediate:true})
const rows = computed(() => (isNutrition.value ? nutrition.value?.items || [] : catalog.value?.items || []).filter(x => !search.value.trim() || x.name.includes(search.value.trim())))
const shownCount = ref(12)
watch(search, () => { shownCount.value = 12 })
const selected = computed(() => selectedCart(quantities.value,checkout.value?.cart))
const itemCount = computed(() => selected.value.reduce((n,x) => n+x.quantity,0))
const labels: Record<string,string> = { needs_selection:'选择取餐方式',awaiting_confirmation:'核对后确认',submitted:'订单已生成',unknown:'订单待核实',submitting:'正在提交',verifying:'正在核实',cancelled:'草案已取消',stale:'需要重新报价',failed:'未能提交' }
const payment = computed(() => paymentPresentation(order.value,now.value))
const expired = computed(() => payment.value.expired)
const payLabel = computed(() => payment.value.label)
function saveToken() {
  if (props.busy) return
  if (!token.value.trim()) { localError.value = '请填写从麦当劳官方平台获取的 MCP Token'; return }
  setStoredMcdToken(token.value.trim()); token.value = ''; localError.value = ''
  tokenPanelChosen = false; tokenOpen.value = false
  emit('commerce', { action:'connect' })
}
function toggleTokenPanel() {
  tokenPanelChosen = true
  tokenOpen.value = !tokenOpen.value
}
function findStores() {
  if (props.busy) return
  if (!city.value.trim() || !keyword.value.trim()) { localError.value = '请填写城市，以及门店或地标'; return }
  localError.value = ''; emit('commerce', { action:'search_stores',city:city.value.trim(),keyword:keyword.value.trim() })
}
async function saveLocation(clear = false) {
  if (props.busy || locationSaving.value) return
  if (!clear && (!city.value.trim() || !keyword.value.trim())) { localError.value='请填写默认城市和地标'; return }
  locationSaving.value=true;localError.value=''
  try {
    savedLocation.value=await api.saveCommercePreferences(clear?'':city.value.trim(),clear?'':keyword.value.trim())
    locationNote.value=clear?'默认位置已清除':'默认位置已保存，聊天未提供城市时可以引用；本轮地点优先。'
  } catch (error) { localError.value=error instanceof Error ? error.message : '默认位置未能保存，请重试。' }
  finally { locationSaving.value=false }
}
function changeMenu() {
  const value=checkout.value
  if (props.busy || !value) return
  emit('commerce',{action:'checkout_menu',target_id:value.id,version:value.version})
}
function showProduct(id: string) {
  emit('commerce',{action:'product_detail',target_id:id,...(menuOpen.value && checkout.value ? {checkout_id:checkout.value.id,version:checkout.value.version} : {})})
}
function showNutrition() {
  if (props.busy) return
  tab.value='nutrition'; search.value=''
  if (!nutrition.value) emit('commerce', { action:'nutrition' })
}
function quantity(id: string, delta: number) {
  if (!props.busy) quantities.value={...quantities.value,[id]:Math.max(0,Math.min(20,(quantities.value[id] || 0)+delta))}
}
function preview() {
  if (props.busy || !selected.value.length || !catalog.value?.store_id) return
  if (menuOpen.value && checkout.value) emit('commerce',{action:'revise_order',target_id:checkout.value.id,version:checkout.value.version,items:selected.value})
  else emit('commerce', { action:'prepare_order',target_id:catalog.value.store_id,items:selected.value })
}
function previewProduct() {
  const product=catalog.value?.product
  if (props.busy || !product?.id || !catalog.value?.store_id) return
  const items=[{product_id:product.id,quantity:1,use_default_options:true}]
  if (menuOpen.value && checkout.value) emit('commerce',{action:'revise_order',target_id:checkout.value.id,version:checkout.value.version,items})
  else emit('commerce',{action:'prepare_order',target_id:catalog.value.store_id,items})
}
function copyOrder() { if (order.value?.order_id) uni.setClipboardData({ data:order.value.order_id }) }
</script>

<template>
  <view class="mcd-card" :class="{ 'mcd-busy': busy }">
    <view class="mcd-head">
      <view class="mcd-brand">
        <view class="mcd-logo"><image src="/static/mcd-official.png" mode="aspectFit" aria-label="麦当劳" /></view>
        <view><text class="mcd-eyebrow">McDONALD’S · 点餐助手</text><text class="mcd-title">好好吃，也好好选。</text><text class="mcd-subtitle">官方营养 · 真实门店 · 付款由你确认</text></view>
      </view>
      <view class="mcd-settings"><button class="mcd-token-toggle" :disabled="busy" @click="locationOpen=!locationOpen">位置设置</button><button class="mcd-token-toggle" :disabled="busy" @click="toggleTokenPanel">{{ connection === 'token_required' ? '连接 Token' : 'Token 设置' }}</button></view>
    </view>

    <view v-if="tokenOpen" class="mcd-token-panel">
      <text class="mcd-section-title">连接你的麦当劳账号</text>
      <text class="mcd-muted">{{ getStoredMcdToken() ? '已在此设备保存 Token，可在下方更换。' : '使用官方 MCP Token，查询门店并创建待支付订单。' }}</text>
      <view class="mcd-token-input"><input v-model="token" :maxlength="512" password :disabled="busy" placeholder="粘贴你的 MCP Token" autocomplete="off" /><button class="mcd-btn mcd-red" :disabled="busy" @click="saveToken">{{ busy ? '连接中…' : '保存并连接' }}</button></view>
      <view class="mcd-guide"><text>还没有 Token？</text><text class="mcd-link" @click="emit('open',guide)">去麦当劳官方平台获取 ↗</text></view>
      <text class="mcd-muted">手机号登录官方平台 → 控制台按提示激活 → 复制 MCP Token。请勿把 Token 发到聊天中。</text>
    </view>
    <view v-if="localError" class="mcd-error" role="alert">{{ localError }}</view>
    <view v-if="result && ['failed','clarification_required','stale'].includes(result.overall_status || '')" class="mcd-error" role="alert">{{ result.reply }}</view>
    <view v-if="result?.commerce_availability" class="mcd-availability" role="status">
      <text class="mcd-section-title">{{ result.commerce_availability.choice ? '先确认这家营业中的餐厅' : '这次点餐需要调整' }}</text>
      <text class="mcd-muted">{{ result.commerce_availability.message }}</text>
      <view class="mcd-local-actions"><button v-for="action in actions.filter(a=>['confirm_tasks','cancel_tasks','commerce_recovery_menu'].includes(a.type))" :key="action.type" class="mcd-btn" :class="action.type==='cancel_tasks' ? 'mcd-outline' : 'mcd-red'" :disabled="busy" @click="emit('action',action)">{{ action.type==='confirm_tasks' && result.commerce_availability.choice ? '确认换店，继续选餐' : action.label }}</button></view>
    </view>
    <view v-if="busy" class="mcd-progress" aria-live="polite">正在处理，请稍候…</view>

    <view class="mcd-tabs"><button :class="{active: !isNutrition}" :disabled="busy" @click="tab='order'">门店与点餐</button><button :class="{active: isNutrition}" :disabled="busy" @click="showNutrition">官方营养库 <text v-if="nutrition">{{ nutrition.items.length }}</text></button></view>

    <view v-if="isNutrition" class="mcd-library">
      <view class="mcd-section-head"><view><text class="mcd-section-title">把营养看明白，再决定吃什么</text><text class="mcd-muted">按官方条目的份量展示，不把营养值当作价格或门店库存。</text></view><button class="mcd-btn mcd-outline" :disabled="busy" @click="emit('ask','结合我的饮食目标和这份官方营养库，帮我推荐适合的一餐，先不要下单。')">让教练帮我选 →</button></view>
      <input v-model="search" class="mcd-search" placeholder="搜索食物，例如：鸡翅、薯条" />
      <view class="mcd-nutrition-grid"><view v-for="food in rows.slice(0,shownCount)" :key="food.name" class="mcd-food">
        <text class="mcd-food-name">{{ food.name }}</text><text v-if="food.portion" class="mcd-muted">官方份量：{{ food.portion }}</text>
        <view class="mcd-calories"><text>{{ food.kcal }}</text> kcal</view><view class="mcd-macros"><text>蛋白 {{ food.protein_g }}g</text><text>脂肪 {{ food.fat_g }}g</text><text>碳水 {{ food.carb_g }}g</text></view>
      </view></view>
      <text v-if="nutrition && !rows.length" class="mcd-empty">没有找到对应的官方营养条目，换个关键词试试。</text>
      <button v-if="rows.length > shownCount" class="mcd-more" @click="shownCount+=12">查看更多营养条目 ↓</button>
      <text v-if="nutrition" class="mcd-footnote">来源：{{ nutrition.source }} · {{ nutrition.fetched_at.replace('T',' ').slice(0,16) }} UTC · {{ nutrition.portion_note }}</text>
    </view>

    <template v-else>
      <view v-if="!checkout || locationOpen" class="mcd-location">
        <text class="mcd-section-title">从你附近的门店开始</text>
        <text class="mcd-muted">{{ savedLocation.city ? `默认位置：${savedLocation.city} · ${savedLocation.keyword}` : '可以预先保存城市和地标，供聊天找店使用。' }} · 到店自取，不是配送地址或实时定位。</text>
        <view class="mcd-location-fields"><input v-model="city" :maxlength="80" :disabled="busy" placeholder="城市，例如北京" @input="locationEdited=true" /><input v-model="keyword" :maxlength="120" :disabled="busy" placeholder="门店或地标，例如三里屯" @input="locationEdited=true" /><button class="mcd-btn mcd-red" :disabled="busy" @click="findStores">查找门店 →</button></view>
        <view class="mcd-location-tools"><button class="mcd-detail-link" :disabled="busy || locationSaving" @click="saveLocation(false)">保存为默认位置</button><button v-if="savedLocation.city" class="mcd-detail-link" :disabled="busy || locationSaving" @click="saveLocation(true)">清除默认位置</button></view><text v-if="locationNote" class="mcd-muted" aria-live="polite">{{ locationNote }}</text>
      </view>
      <view v-if="catalog?.kind==='stores' && !checkout" class="mcd-stores">
        <view class="mcd-section-head"><text class="mcd-muted">{{ catalog.items.length }} 家门店 · 距离由官方接口返回</text><button class="mcd-btn mcd-outline" :disabled="busy" @click="emit('ask','从刚才这些门店里，帮我选择距离最近且正在营业的一家，再查看它的菜单。')">帮我选一家 →</button></view>
        <view v-for="store in catalog.items" :key="store.id" class="mcd-store-row"><view class="mcd-store-main"><text class="mcd-item-title">{{ store.name }}</text><view class="mcd-store-meta"><text v-if="store.distance_m != null">{{ store.distance_m }} m</text><text>营业时间 {{ store.hours && store.hours!=='–' ? store.hours : '官方未提供' }}</text><text class="mcd-status" :class="{closed:!store.open}">{{ store.open ? '营业中' : store.availability==='unknown' ? '营业状态待确认' : '休息中' }}</text></view><text class="mcd-muted">{{ store.address }}</text></view><button class="mcd-btn mcd-row-choice" :disabled="busy || !store.open" @click="emit('commerce',{action:'menu',target_id:store.id})">{{ store.open ? '选这家 →' : '暂未营业' }}</button></view>
        <text v-if="!catalog.items.length" class="mcd-empty">没有查到门店，换个地标或搜索范围试试。</text>
      </view>
      <view v-if="catalog?.kind==='products' && (!checkout || menuOpen)" class="mcd-menu">
        <view class="mcd-menu-store"><text class="mcd-section-title">{{ catalog.store?.name || '门店当前菜单' }}</text><text class="mcd-muted">{{ catalog.store?.address }} <text v-if="catalog.store?.hours">· {{ catalog.store.hours }}</text></text></view>
        <input v-model="search" class="mcd-search" placeholder="搜索这家店的餐品" />
        <StableScroll class="mcd-menu-scroll" :scroll-y="true" :chain-to-parent="true">
        <view v-for="item in rows" :key="item.id" class="mcd-product-row" :class="{'mcd-product-selected':quantities[item.id]>0}"><view class="mcd-product-main"><text class="mcd-item-title">{{ item.name }} <text v-if="quantities[item.id]>0" class="mcd-selected-tag">已选</text></text><text class="mcd-product-price">¥{{ item.menu_price ?? '—' }} <text class="mcd-muted">菜单价</text></text><view v-if="item.nutrition" class="mcd-muted">官方条目 {{ item.nutrition.portion }} · {{ item.nutrition.kcal }} kcal · 蛋白 {{ item.nutrition.protein_g }}g · 脂肪 {{ item.nutrition.fat_g }}g · 碳水 {{ item.nutrition.carb_g }}g</view><text v-if="item.product_type===2" class="mcd-muted">默认套餐搭配，报价时展示具体组成</text><button class="mcd-detail-link" :disabled="busy" @click="showProduct(item.id)">查看商品详情 →</button></view><view class="mcd-quantity"><button :disabled="busy || !quantities[item.id]" @click="quantity(item.id,-1)">−</button><text>{{ quantities[item.id] || 0 }}</text><button :disabled="busy || quantities[item.id]>=20" @click="quantity(item.id,1)">＋</button></view></view>
        </StableScroll>
        <view class="mcd-cart-footer"><text class="mcd-muted">已选 {{ itemCount }} 份 · 最终金额以官方报价为准</text><button v-if="checkout && menuOpen" class="mcd-btn mcd-outline" :disabled="busy" @click="menuOpen=false">保留原预览</button><button class="mcd-btn mcd-red" :disabled="busy || !itemCount" @click="preview">{{ checkout && menuOpen ? '更新订单预览 →' : '查看订单预览 →' }}</button></view>
      </view>
      <view v-if="catalog?.kind==='detail' && catalog.product && (!checkout || menuOpen)" class="mcd-detail">
        <text class="mcd-section-title">{{ catalog.product.name }}</text>
        <text class="mcd-muted">{{ catalog.store?.name }} · 菜单参考价 ¥{{ catalog.product.menu_price ?? '—' }}</text>
        <text v-if="catalog.detail?.option_group_count===0" class="mcd-detail-note">此商品未返回额外可选项，可直接沿用当前商品生成报价预览。</text>
        <view v-for="group in catalog.detail?.round_groups || []" :key="group.id" class="mcd-detail-group"><text class="mcd-item-title">{{ group.name }}</text><text class="mcd-muted">{{ group.choices.map(x=>x.name+(x.default ? '（默认）' : '')).join('、') }}</text></view>
        <view v-for="(group,index) in catalog.detail?.modification_groups || []" :key="index" class="mcd-detail-group"><text class="mcd-item-title">{{ group.name }}</text><view class="mcd-detail-choices"><text v-for="(choice,i) in group.choices" :key="i" :class="{selected:choice.selected_quantity>0}">{{ choice.name }}{{ choice.selected_quantity>0 ? ' · 默认' : '' }}</text></view></view>
        <view class="mcd-cart-footer"><text class="mcd-muted">预览 1 份，沿用官方默认选项；不会提交订单。</text><button class="mcd-btn mcd-red" :disabled="busy" @click="previewProduct">生成报价预览 →</button></view>
      </view>
      <view v-if="!checkout && !result?.commerce_availability && result?.pending_confirmation?.status==='pending' && actions.length" class="mcd-detail"><text class="mcd-muted">{{ result.pending_confirmation.question }}</text><view class="mcd-local-actions"><button v-for="action in actions" :key="action.type" class="mcd-btn mcd-outline" :disabled="busy" @click="emit('action',action)">{{ action.label }}</button></view></view>
      <view v-if="catalog?.kind==='options' && !checkout"><view v-for="item in catalog.items" :key="item.id" class="mcd-store-row"><view><text class="mcd-item-title">{{ item.name }}</text><text class="mcd-muted">{{ item.round_name }}</text></view><button class="mcd-btn mcd-outline" :disabled="busy" @click="emit('ask',`我要选择刚才套餐里的${item.name}，请为我更新报价预览，先别提交订单。`)">选择这项 →</button></view></view>

      <view v-if="checkout" class="mcd-checkout">
        <view v-if="conflict" class="mcd-existing" role="status">
          <text class="mcd-section-title">{{ conflict.orders.some(x=>x.order.lifecycle==='unpaid') ? '你还有未支付订单' : '有一笔提交结果需要留意' }}</text>
          <text class="mcd-muted">本次新预览已保留；支付旧单、取消旧单或另下新单由你选择。</text>
          <view v-for="old in conflict.orders" :key="old.checkout_id" class="mcd-existing-order">
            <text class="mcd-item-title">{{ old.order.lifecycle==='unpaid' ? '待支付' : old.in_flight ? '正在提交' : '结果待核实' }} · {{ old.store_name }}</text>
            <text class="mcd-muted">{{ old.cart.map(x=>`${x.name} × ${x.quantity}`).join('、') }}</text>
            <text v-if="old.order.order_id" class="mcd-muted">订单号 {{ old.order.order_id }}</text>
            <text v-if="old.order.payment_deadline && old.order.lifecycle==='unpaid'" class="mcd-muted">支付截止 {{ new Date(old.order.payment_deadline).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false})+'（北京时间）' }}</text>
            <text v-if="old.order.lifecycle==='unknown'" class="mcd-muted">结果未知不能按15分钟推断过期。另下新单不会重发或取消旧请求。</text>
            <view class="mcd-local-actions">
              <button v-if="paymentPresentation(old.order,now).canPay && old.order.lifecycle==='unpaid'" class="mcd-btn mcd-red" :disabled="busy" @click="emit('open',old.order.pay_url!)">继续支付旧订单 →</button>
              <text v-else-if="old.order.lifecycle==='unpaid'" class="mcd-muted">未返回付款链接，请刷新查询或前往麦当劳 App。</text>
              <button v-for="action in conflictAction('checkout_existing_cancel',old.checkout_id)" :key="action.existing_checkout_id" class="mcd-btn mcd-outline" :disabled="busy" @click="emit('action',action)">取消这笔旧单</button>
            </view>
          </view>
          <view class="mcd-local-actions mcd-existing-decisions"><button v-for="action in actions.filter(a=>['checkout_existing_query','checkout_place_new'].includes(a.type))" :key="action.type" class="mcd-btn" :class="action.type==='checkout_place_new' ? 'mcd-red' : 'mcd-outline'" :disabled="busy" @click="emit('action',action)">{{ action.label }}</button></view>
        </view>
        <view class="mcd-picked"><text class="mcd-eyebrow">{{ order?.order_id ? '这次的餐品' : '已为你选好，核对后再下单' }}</text><text class="mcd-picked-name">{{ checkout.cart.map(item=>`${item.name} × ${item.quantity}`).join('、') }}</text><button v-if="['awaiting_confirmation','needs_selection'].includes(checkout.status)" class="mcd-detail-link" :disabled="busy" @click="changeMenu">不满意？换餐品 / 查看完整菜单 →</button></view>
        <view class="mcd-section-head"><view><text class="mcd-section-title">{{ order?.order_id ? '你的麦当劳订单' : '核对这份美味' }}</text><text class="mcd-muted">{{ checkout.quote.store.name }}</text></view><text class="mcd-order-state">{{ labels[checkout.status] || checkout.status }}</text></view>
        <text class="mcd-muted">{{ checkout.quote.store.address }} · 到店自取</text>
        <view class="mcd-order-items"><view v-for="item in checkout.cart" :key="item.product_id" class="mcd-order-item"><text class="mcd-item-title">{{ item.name }} <text class="mcd-item-count">× {{ item.quantity }}</text></text><text v-if="item.specifications.length" class="mcd-muted">每份组成：{{ item.specifications.map(s=>`${s.name}×${s.quantity}`).join('、') }}</text></view></view>
        <view class="mcd-order-total"><view><text class="mcd-muted">官方总额</text><text class="mcd-muted">不需要餐具 · 不会自动付款</text></view><text class="mcd-total-amount"><text>¥</text>{{ (checkout.quote.total_cents/100).toFixed(2) }}</text></view>
        <view class="mcd-pickup"><view><text class="mcd-item-title">取餐方式</text><text class="mcd-muted">{{ checkout.quote.take_ways.find(w=>w.code===checkout.take_way_code)?.title || '请选择堂食或外带' }}{{ checkout.quote.take_way_source==='default_takeaway' ? ' · 已默认外带，可在此修改' : '' }}</text></view><view class="mcd-local-actions"><button v-for="action in actions.filter(a=>a.type==='checkout_way')" :key="action.take_way_code" class="mcd-btn mcd-outline" :class="{'mcd-way-selected':action.take_way_code===checkout.take_way_code}" :disabled="busy" @click="emit('action',action)">{{ action.label }}</button></view></view>
        <text class="mcd-footnote">{{ checkout.quote.fees_note }}</text>
        <view v-if="order?.order_id" class="mcd-order-receipt"><text class="mcd-section-title">{{ payment.cancelled ? '订单已取消' : expired ? '支付期限已过，不阻塞新下单' : order.status || '订单状态待核实' }}</text><text class="mcd-muted">订单号 {{ order.order_id }}</text><text v-if="order.expire_at && !payment.closed && !expired" class="mcd-muted">请在 {{ order.expire_at }} 前完成支付</text><text v-if="order.verified===false && !payment.closed && !expired" class="mcd-verification-note">订单号已返回，部分信息仍待核实。请在官方页面核对餐品与金额，勿重复下单。</text><view class="mcd-receipt-actions"><button v-if="order.pay_url" class="mcd-btn mcd-red mcd-pay" :disabled="busy" @click="emit('open',order.pay_url)">{{ payLabel }}</button><text v-else class="mcd-muted">官方暂未返回付款链接，请查询订单，或前往麦当劳 App 核对。</text><button class="mcd-btn mcd-outline" @click="copyOrder">复制订单号</button></view></view>
        <view v-if="!conflict && actions.some(a=>a.type!=='checkout_way')" class="mcd-checkout-actions"><text class="mcd-muted">{{ checkout.status==='awaiting_confirmation' ? '确认后创建待支付订单；变价需重新确认。' : '查看状态，不会重复创建订单。' }}</text><view class="mcd-local-actions"><button v-for="action in actions.filter(a=>a.type!=='checkout_way')" :key="action.type" class="mcd-btn" :class="action.type==='confirm_tasks' ? 'mcd-red':'mcd-outline'" :disabled="busy" @click="emit('action',action)">{{ busy ? '处理中…' : action.label }}</button></view></view>
      </view>
    </template>
    <view class="mcd-footer"><text class="mcd-footer-dot"></text><text>营养与点餐数据来自麦当劳官方接口 · 本应用为独立教练助手</text></view>
  </view>
</template>

<style scoped>
.mcd-availability{margin:16px 24px;padding:18px;border:1px solid #ebcc91;background:#fff8e7;border-radius:16px}.mcd-availability .mcd-muted{margin-top:8px;line-height:1.9}.mcd-availability .mcd-local-actions{margin-top:14px;justify-content:flex-end;flex-wrap:wrap}

.mcd-existing{padding:18px;background:#fff8e7;border:1px solid #ebcc91;border-radius:16px;margin-bottom:20px}.mcd-existing-order{padding:14px 0;border-bottom:1px solid #edddbe}.mcd-existing .mcd-local-actions{margin-top:12px;flex-wrap:wrap}.mcd-existing-decisions{justify-content:flex-end}

.mcd-detail{padding:20px 24px}.mcd-detail-note{display:block;color:#927558;font-size:12px;line-height:1.8;margin-top:12px}.mcd-detail-group{padding:14px 0;border-bottom:1px solid #f1e7dd}.mcd-detail-choices{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}.mcd-detail-choices text{padding:5px 10px;background:#f8f3ed;border-radius:16px;font-size:11px;color:#9a8674}.mcd-detail-choices .selected{background:#fff0dc;color:#aa5a21}.mcd-detail-link{display:inline-block;margin:6px 0 0;padding:0;background:none;border:0;line-height:1.8;color:#a84423;font-size:11px}.mcd-detail-link::after{border:0}

.mcd-card{border:1px solid #f0dfcf;border-radius:22px;background:#fff;overflow:hidden;color:#2c211b;box-shadow:0 8px 30px #542c0e08;margin-bottom:16px}
.mcd-head{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:24px;background:linear-gradient(110deg,#fff8eb,#fffdf8 65%,#fff0e9)}
.mcd-brand{display:flex;align-items:center;gap:14px;min-width:0}
.mcd-logo{background:#da291c;border-radius:16px;width:60px;height:60px;flex-shrink:0;display:flex;align-items:center;justify-content:center}
.mcd-logo image{width:42px;height:42px}
.mcd-eyebrow,.mcd-title,.mcd-subtitle,.mcd-section-title,.mcd-muted,.mcd-item-title,.mcd-food-name,.mcd-footnote{display:block}
.mcd-eyebrow{font-size:10px;letter-spacing:1.4px;font-weight:800;color:#a85b21}
.mcd-title{font-size:23px;font-weight:850;letter-spacing:-.6px;line-height:1.7}
.mcd-subtitle{font-size:12px;color:#987e69}
.mcd-brand>view:last-child{min-width:0}
.mcd-token-toggle{background:#fff9;border:1px solid #ead4bd;border-radius:18px;font-size:11px;line-height:30px;padding:0 12px;color:#8b5c39;margin:0;flex-shrink:0}
.mcd-tabs{display:flex;gap:6px;padding:12px 24px;border-bottom:1px solid #f3ebe3}
.mcd-tabs button{background:none;color:#9a826d;font-size:12px;padding:7px 14px;border-radius:10px;line-height:1.5;margin:0;font-weight:700}
.mcd-tabs button.active{color:#a3291e;background:#fff0e9}
.mcd-tabs button text{margin-left:4px;font-size:10px;opacity:.7}
.mcd-btn{font-size:12px;font-weight:750;line-height:1.5;padding:10px 17px;border-radius:24px;margin:0;white-space:nowrap;cursor:pointer;transition:background .15s,transform .15s;flex-shrink:0;display:inline-flex;align-items:center;justify-content:center;min-height:38px;box-sizing:border-box}
.mcd-btn:active{transform:translateY(1px)}button::after{border:0}
.mcd-red{background:#da291c;color:#fff;box-shadow:0 4px 12px #da291c14}
.mcd-outline{background:#fff;border:1px solid #e9cdb8;color:#9a3823}
.mcd-btn[disabled],button[disabled]{opacity:.48;cursor:default}
.mcd-muted{color:#947f6d;font-size:12px;line-height:1.8}
.mcd-section-title{font-size:16px;font-weight:800;line-height:1.6}
.mcd-item-title{font-size:14px;font-weight:750;line-height:1.7}
.mcd-section-head{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:14px}
.mcd-token-panel{background:#fffcf5;border-top:1px solid #f3e1bf;padding:20px 24px}
.mcd-token-input{display:flex;gap:10px;margin:12px 0}
.mcd-token-input input,.mcd-location-fields input,.mcd-search{border:1px solid #eaded1;background:#fdfaf5;border-radius:12px;padding:10px 12px;min-height:40px;font-size:13px;box-sizing:border-box}
.mcd-token-input input{flex:1;min-width:0}
.mcd-guide{display:flex;flex-wrap:wrap;gap:8px;font-size:12px;margin:10px 0}
.mcd-link{color:#b4291d;font-weight:700;cursor:pointer}
.mcd-error{margin:12px 24px;padding:10px 12px;background:#fff0eb;border-radius:9px;color:#b83222;font-size:12px}
.mcd-progress{padding:9px 24px;background:#fff8df;color:#9c6c12;font-size:12px}
.mcd-location,.mcd-library,.mcd-stores,.mcd-menu,.mcd-checkout{padding:20px 24px}
.mcd-location{padding-bottom:8px}
.mcd-settings,.mcd-location-tools{display:flex;gap:10px;flex-wrap:wrap}.mcd-settings{justify-content:flex-end}.mcd-location-tools{margin-top:8px}
.mcd-menu-scroll{height:320px;max-height:40vh;margin-top:12px;scrollbar-gutter:stable;border:1px solid #f0e3d7;border-radius:14px;padding:0 12px;box-sizing:border-box}
.mcd-picked{background:#fff5df;border:1px solid #f0dab0;border-radius:18px;padding:18px;margin-bottom:18px}.mcd-picked-name{display:block;font-size:24px;font-weight:800;color:#982b19;margin:8px 0;line-height:1.5}
.mcd-product-selected{background:#fff9eb}.mcd-selected-tag{color:#ad3921;font-size:11px;border-radius:10px;background:#ffe8b9;padding:3px 7px}.mcd-way-selected{background:#fff1cf!important;border-color:#c26a37!important;color:#9a301c!important}
.mcd-location-fields{display:flex;gap:8px;margin-top:10px}
.mcd-location-fields input{min-width:0;flex:1}
.mcd-location-fields input:first-child{max-width:150px}
.mcd-store-row{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:18px 0;border-bottom:1px solid #f1e7dd}
.mcd-store-row:last-child{border-bottom:0}
.mcd-store-main{min-width:0}
.mcd-store-meta{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:12px;color:#947f6d;margin:4px 0}
.mcd-status{border-radius:20px;background:#edf5ec;color:#57754c;font-size:10px;padding:1px 8px}
.mcd-status.closed{background:#f2eee9;color:#9e958a}
.mcd-row-choice{color:#ba3521;background:#fff1e8}
.mcd-search{width:100%;margin:12px 0 4px}
.mcd-nutrition-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin-top:12px}
.mcd-food{padding:16px;border:1px solid #f1e6d9;background:#fffdf8;border-radius:14px}
.mcd-food-name{font-size:14px;font-weight:750}
.mcd-calories{font-size:11px;color:#a0401f;margin:12px 0 8px}
.mcd-calories>text{font-size:25px;font-weight:800;margin-right:3px}
.mcd-macros{display:flex;gap:10px;flex-wrap:wrap;font-size:10px;color:#967f68}
.mcd-more{margin:16px auto 0;border:1px solid #eadaca;background:#fff;border-radius:20px;padding:8px 18px;font-size:12px;line-height:1.5;color:#8d6649}
.mcd-footnote{font-size:10px;line-height:1.8;color:#a38e7b;margin-top:12px}
.mcd-menu-store{margin-bottom:10px}
.mcd-product-row{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:15px 0;border-bottom:1px solid #f3e9df}
.mcd-product-main{min-width:0}
.mcd-product-price{display:block;color:#b53721;font-size:16px;font-weight:750;margin:5px 0}
.mcd-product-price .mcd-muted{display:inline;font-size:10px;font-weight:400}
.mcd-quantity{display:flex;align-items:center;gap:10px;flex-shrink:0;font-size:13px;font-weight:700}
.mcd-quantity button{width:30px;height:30px;line-height:28px;border:1px solid #ead6bf;border-radius:50%;padding:0;margin:0;background:#fff9ec;color:#ad3921;font-size:16px}
.mcd-cart-footer{display:flex;align-items:center;justify-content:space-between;gap:16px;padding-top:18px}
.mcd-order-state{background:#fff3ce;color:#9d6718;border-radius:20px;padding:5px 10px;font-size:10px;white-space:nowrap}
.mcd-order-items{margin-top:16px;background:#fffcf6;border-radius:14px;padding:0 16px}
.mcd-order-item{padding:14px 0;border-bottom:1px solid #efe4d7}
.mcd-order-item:last-child{border-bottom:0}
.mcd-item-count{font-size:12px;font-weight:500;color:#a1795c}
.mcd-order-total{display:flex;justify-content:space-between;align-items:center;margin:18px 0}
.mcd-total-amount{font-size:32px;font-weight:850;color:#b62c1f;letter-spacing:-1px}
.mcd-total-amount>text{font-size:19px;margin-right:3px}
.mcd-pickup{padding:14px 0;display:flex;justify-content:space-between;align-items:center;gap:12px;border-top:1px solid #f0e6dc}
.mcd-local-actions{display:flex;justify-content:flex-end;gap:8px;flex-wrap:wrap}
.mcd-checkout-actions{border-top:1px solid #eee1d3;display:flex;align-items:center;justify-content:space-between;gap:14px;padding-top:18px;margin-top:18px}
.mcd-checkout-actions>.mcd-muted{max-width:42%;font-size:10px}
.mcd-order-receipt{padding:18px;background:#fff2e9;border:1px solid #f2d4bd;border-radius:14px;margin-top:18px}
.mcd-receipt-actions{display:flex;justify-content:flex-end;gap:8px;flex-wrap:wrap;margin-top:16px}
.mcd-pay{min-width:190px}
.mcd-verification-note{display:block;color:#a36c32;font-size:11px;line-height:1.8;margin-top:10px}
.mcd-footer{display:flex;align-items:center;gap:6px;background:#fcfaf7;border-top:1px solid #f4ebe2;padding:12px 24px;font-size:10px;color:#a58f7c}
.mcd-footer-dot{width:5px;height:5px;border-radius:50%;background:#c7a45f;flex-shrink:0}
.mcd-empty{display:block;color:#9a8570;font-size:12px;padding:20px 0}
@media(max-width:600px){.mcd-head{padding:18px 14px;gap:8px;align-items:flex-start}
.mcd-logo{width:44px;height:44px;border-radius:12px}
.mcd-logo image{width:32px;height:32px}
.mcd-brand{gap:10px}
.mcd-title{font-size:18px;line-height:1.45}
.mcd-eyebrow{font-size:8px;letter-spacing:.6px}
.mcd-subtitle{font-size:10px}
.mcd-token-toggle{font-size:10px;padding:0 9px;line-height:27px}
.mcd-tabs,.mcd-footer{padding:10px 14px}
.mcd-location,.mcd-library,.mcd-stores,.mcd-menu,.mcd-checkout,.mcd-token-panel{padding:16px 14px}
.mcd-location-fields{flex-wrap:wrap}
.mcd-location-fields input:first-child{max-width:100px}
.mcd-location-fields .mcd-btn{margin-left:auto}
.mcd-section-head{align-items:flex-start;gap:10px}
.mcd-section-title{font-size:14px}
.mcd-section-head>.mcd-btn{font-size:10px;padding:8px 12px}
.mcd-item-title{font-size:13px}
.mcd-store-row{gap:10px}
.mcd-btn{font-size:11px;padding:9px 13px}
.mcd-nutrition-grid{grid-template-columns:1fr}
.mcd-pickup{align-items:center}
.mcd-local-actions{gap:6px}
.mcd-checkout-actions{align-items:flex-start;flex-direction:column}
.mcd-checkout-actions>.mcd-muted{max-width:none}
.mcd-checkout-actions>.mcd-local-actions{align-self:flex-end}
.mcd-token-input{flex-direction:column}
.mcd-token-input .mcd-btn{align-self:flex-end}
.mcd-store-meta{font-size:10px}
.mcd-muted{font-size:11px}
.mcd-receipt-actions{justify-content:flex-end}
.mcd-pay{min-width:170px}}
</style>
