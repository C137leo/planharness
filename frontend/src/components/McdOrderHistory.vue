<script setup lang="ts">
import {computed,onMounted,onUnmounted,ref} from 'vue'
import StableScroll from './StableScroll.vue'
import {api} from '../api/client'
import type {CommerceHistoryOrder,CommerceOrderGroup} from '../api/types'
import {paymentPresentation} from '../styles/commerceUi'
const emit=defineEmits<{(e:'close'):void;(e:'open',url:string):void}>()
const group=ref<CommerceOrderGroup>('active'),items=ref<CommerceHistoryOrder[]>([]),selected=ref<CommerceHistoryOrder|null>(null)
const counts=ref({active:0,paid:0,inactive:0,unknown:0}),cursor=ref(''),loading=ref(false),syncing=ref(false),detailLoading=ref(false)
const error=ref(''),note=ref(''),asOf=ref(''),now=ref(Date.now())
let alive=true,listRevision=0,detailRevision=0,timer:ReturnType<typeof setInterval>|undefined
const historical=computed(()=>group.value==='paid'||group.value==='inactive')
const payment=computed(()=>{const value=paymentPresentation(selected.value?.order,now.value);return {...value,expired:selected.value?.group==='unknown'?false:value.expired,canPay:selected.value?.group==='active'&&value.canPay}})
const detailStatus=computed(()=>payment.value.expired&&selected.value?.group==='active'?'已过期':selected.value?.status_label)
const dialog=ref<unknown>(null)
let previousFocus:HTMLElement|null=null
let dialogElement:HTMLElement|null=null
const titles={active:'等待你付款的美味',paid:'已经付款的订单',inactive:'已失效的订单与提交记录',unknown:'先核对，再决定'}
const empty={active:'暂时没有有效的待支付订单',paid:'还没有已付款的订单记录',inactive:'暂无已失效记录',unknown:'没有待核实的提交记录'}
function money(value:number|null){return value==null?'金额未保存':'¥'+(value/100).toFixed(2)}
function time(value?:string){if(!value)return '尚未同步';try{return new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false})}catch{return value}}
function failure(value:unknown){return value instanceof Error?value.message:'暂时未能读取订单，请重试。'}
async function load(append=false){
  if(append && (loading.value||!cursor.value))return
  const version=++listRevision,current=group.value,position=append?cursor.value:''
  loading.value=true;error.value=''
  try{
    const page=await api.commerceOrders(current,position)
    if(!alive||version!==listRevision||current!==group.value)return
    items.value=append?[...items.value,...page.items.filter(x=>!items.value.some(v=>v.id===x.id))]:page.items
    counts.value=page.counts;cursor.value=page.next_cursor;asOf.value=page.as_of
  }catch(value){if(alive&&version===listRevision)error.value=failure(value)}
  finally{if(alive&&version===listRevision)loading.value=false}
}
function chooseGroup(value:CommerceOrderGroup){
  if(group.value===value)return
  group.value=value;selected.value=null;detailRevision++;items.value=[];cursor.value='';note.value='';void load()
}
async function selectOrder(value:CommerceHistoryOrder){
  selected.value=value;const version=++detailRevision;detailLoading.value=true;error.value=''
  try{const data=await api.commerceOrderDetail(value.id);if(alive&&version===detailRevision)selected.value=data}
  catch(errorValue){if(alive&&version===detailRevision)error.value=failure(errorValue)}
  finally{if(alive&&version===detailRevision)detailLoading.value=false}
}
async function syncAll(){
  if(syncing.value)return
  syncing.value=true;error.value='';note.value=''
  try{
    const result=await api.syncCommerceOrders()
    if(!alive)return
    note.value=result.checked ? `已检查 ${result.checked} 笔${result.failed?'，其中 '+result.failed+' 笔暂未同步':''}${result.remaining?'；还有 '+result.remaining+' 笔可继续同步':''}` : '当前没有需要同步的待支付或待核实订单。'
    await load()
    if(selected.value)await selectOrder(selected.value)
  }catch(value){if(alive)error.value=failure(value)}
  finally{if(alive)syncing.value=false}
}
async function refreshOne(){
  const value=selected.value;if(!value||detailLoading.value||syncing.value)return
  const version=++detailRevision;detailLoading.value=true;error.value=''
  try{
    const data=await api.refreshCommerceOrder(value.id)
    if(!alive)return
    if(version===detailRevision)selected.value=data
    await load()
    note.value='这笔订单状态已同步。'
  }catch(errorValue){if(alive&&version===detailRevision)error.value=failure(errorValue)}
  finally{if(alive&&version===detailRevision)detailLoading.value=false}
}
function copyNumber(){if(selected.value?.order.order_id)uni.setClipboardData({data:selected.value.order.order_id})}
function followOrder(){const url=selected.value?.order.pay_url;if(url&&/^https:\/\//i.test(url))emit('open',url)}
function guardWheel(event:WheelEvent){
  // #ifdef H5
  if(!(event.target as HTMLElement)?.closest?.('[data-stable-scroll]'))event.preventDefault()
  // #endif
}
function escape(event:KeyboardEvent){
  if(event.key==='Escape'){event.preventDefault();emit('close')}
  // #ifdef H5
  if(event.key==='Tab'){
    const value=dialog.value as {$el?:HTMLElement}|HTMLElement|null
    const el=value&&('$el' in value?value.$el:value) as HTMLElement|null
    const buttons=el?[...el.querySelectorAll<HTMLElement>('button,uni-button,[tabindex]:not([tabindex="-1"])')].filter(x=>!x.hasAttribute('disabled')&&x.getBoundingClientRect().height>0):[]
    const first=buttons[0],last=buttons[buttons.length-1]
    if(first&&last&&(event.shiftKey&&document.activeElement===first||!event.shiftKey&&document.activeElement===last)){
      event.preventDefault();(event.shiftKey?last:first).focus()
    }
  }
  // #endif
}
onMounted(async()=>{
  // #ifdef H5
  previousFocus=document.activeElement as HTMLElement|null
  document.addEventListener('keydown',escape)
  const target=dialog.value as {$el?:HTMLElement}|HTMLElement|null
  const element=target&&('$el' in target?target.$el:target) as HTMLElement|null
  // Use a native event: uni-app wraps view event targets without DOM closest().
  dialogElement=element
  element?.addEventListener('wheel',guardWheel,{passive:false})
  element?.focus({preventScroll:true})
  // #endif
  await load()
  if(!alive)return
  if(items.value.some(x=>x.can_refresh&&(x.group==='active'||x.group==='unknown')))void syncAll()
  timer=setInterval(()=>{
    now.value=Date.now()
    if(!loading.value&&items.value.some(x=>x.group==='active'&&paymentPresentation(x.order,now.value).expired))void load()
  },15000)
})
onUnmounted(()=>{alive=false;listRevision++;detailRevision++;if(timer)clearInterval(timer)
  // #ifdef H5
  document.removeEventListener('keydown',escape)
  dialogElement?.removeEventListener('wheel',guardWheel)
  dialogElement=null
  if(previousFocus?.isConnected)previousFocus.focus({preventScroll:true})
  // #endif
})
</script>

<template>
  <view class="order-history-overlay" @click.self="emit('close')" @wheel.self.prevent @touchmove.self.prevent>
    <view ref="dialog" class="order-history-dialog" role="dialog" tabindex="-1" aria-modal="true" aria-label="我的麦当劳订单">
      <view class="order-history-header"><view class="order-history-brand"><view class="order-history-logo"><image src="/static/mcd-official.png" mode="aspectFit" /></view><view><text class="order-history-eyebrow">MY McDONALD’S · ORDER BOOK</text><text class="order-history-title">每一单，都有记录。</text><text class="order-history-subtitle">本应用保存的订单 · 可同步官方状态。</text></view></view><button class="order-history-close" aria-label="关闭订单列表" @click="emit('close')">×</button></view>
      <view class="order-history-toolbar"><view class="order-history-tabs"><button :class="{active:!historical}" @click="chooseGroup('active')">有效订单 <text>{{ counts.active }}</text></button><button :class="{active:historical}" @click="chooseGroup('paid')">历史订单 <text>{{ counts.paid }}</text></button></view><button class="order-history-sync" :disabled="syncing" @click="syncAll">{{ syncing?'正在同步…':'↻ 同步官方状态' }}</button></view>
      <view v-if="historical" class="order-history-filters"><button :class="{active:group==='paid'}" @click="chooseGroup('paid')">已付款 {{ counts.paid }}</button><button :class="{active:group==='inactive'}" @click="chooseGroup('inactive')">已失效 {{ counts.inactive }}</button><text>失效记录保留供核对，不计入有效订单。</text></view>
      <view v-if="!historical&&counts.unknown" class="order-history-unknown"><text>{{ counts.unknown }} 笔提交结果待核实，不会当作已过期。</text><button @click="chooseGroup(group==='unknown'?'active':'unknown')">{{ group==='unknown'?'返回待支付':'查看待核实 →' }}</button></view>
      <view v-if="error" class="order-history-error" role="alert">{{ error }} <button :disabled="loading" @click="load()">重试列表</button></view>
      <view v-if="note" class="order-history-note" role="status">{{ note }}</view>
      <view class="order-history-body" :class="{'detail-open':selected}">
        <view class="order-history-list-panel"><view class="order-history-list-heading"><text>{{ titles[group] }}</text><text>{{ loading?'读取中…':'最新在前' }}</text></view>
          <StableScroll class="order-history-list-scroll" :scroll-y="true">
            <view v-for="item in items" :key="item.id" class="order-history-item" :class="{'selected':selected?.id===item.id}" @click="selectOrder(item)">
              <view class="order-history-item-top"><text class="order-history-status" :class="item.group">{{ item.status_label }}</text><text class="order-history-time">{{ time(item.created_at) }}</text></view>
              <text class="order-history-store">{{ item.store.name || '麦当劳订单' }}</text><text class="order-history-foods">{{ item.items.length?item.items.map(x=>x.name+(x.quantity?' × '+x.quantity:'')).join('、'):'旧版记录 · 餐品明细待同步' }}</text>
              <view class="order-history-item-bottom"><text>{{ money(item.total_cents) }}</text><text>查看详情 ↗</text></view>
            </view>
            <view v-if="!items.length" class="order-history-empty"><text class="order-history-empty-icon">▤</text><text>{{ loading?'正在整理你的订单…':empty[group] }}</text><text v-if="!loading">这里展示通过本应用保存的订单与提交记录。</text></view>
            <button v-if="cursor" class="order-history-more" :disabled="loading" @click="load(true)">{{ loading?'加载中…':'加载更多订单 ↓' }}</button>
          </StableScroll>
        </view>
        <view class="order-history-detail-panel">
          <template v-if="selected"><view class="order-history-detail-heading"><button class="order-history-back" @click="selected=null;detailRevision++">← 返回列表</button><text>订单详情</text><button class="order-history-refresh-one" :disabled="!selected.can_refresh||detailLoading||syncing" @click="refreshOne">{{ detailLoading?'同步中…':'↻ 同步这笔' }}</button></view>
            <StableScroll class="order-history-detail-scroll" :scroll-y="true"><view class="order-history-detail-content">
              <view class="order-history-detail-state"><text class="order-history-status" :class="payment.expired?'inactive':selected.group">{{ detailStatus }}</text><text>{{ selected.amount_label }}</text><text class="order-history-detail-price">{{ money(selected.total_cents) }}</text></view>
              <text class="order-history-detail-store">{{ selected.store.name || '麦当劳' }}</text><text class="order-history-detail-muted">{{ selected.store.address || '旧记录未保存门店地址' }}{{ selected.take_way?' · '+selected.take_way:'' }}</text>
              <view class="order-history-detail-meta"><view><text>下单时间</text><text>{{ time(selected.created_at) }}</text></view><view><text>订单号</text><text class="order-history-number">{{ selected.order.order_id || '尚未返回，请在官方账号核对' }}</text></view><view v-if="selected.group==='active'&&selected.order.payment_deadline"><text>支付截止</text><text>{{ time(selected.order.payment_deadline) }}</text></view><view><text>最近同步</text><text>{{ time(selected.order.observed_at) }}</text></view></view>
              <view class="order-history-detail-items"><text class="order-history-section">这一单的餐品</text><view v-for="(item,index) in selected.items" :key="index" class="order-history-detail-food"><view><text>{{ item.name }}</text><text v-if="item.specifications.length" class="order-history-detail-muted">{{ item.specifications.map(x=>x.name+' × '+x.quantity).join('、') }}</text></view><text>× {{ item.quantity ?? '—' }}</text></view><text v-if="!selected.items.length" class="order-history-detail-muted">旧版记录未保存餐品明细，可同步后查询；不会用当前菜单补造历史内容。</text></view>
              <text v-if="selected.needs_connection" class="order-history-detail-tip">可查看已保存记录。同步需要连接下单时的麦当劳账号凭证。</text><text v-else-if="selected.group==='unknown'" class="order-history-detail-tip">提交结果仍待核实，时间过去不代表订单已取消。请同步状态或在麦当劳账号查看。</text><text v-else-if="selected.group==='inactive'||payment.expired" class="order-history-detail-tip">此记录已失效，不影响你创建新的订单。</text><text v-else class="order-history-detail-tip">{{ selected.sync_note }}</text>
            </view></StableScroll>
            <view class="order-history-detail-footer"><button v-if="selected.order.order_id" class="order-history-outline" @click="copyNumber">复制订单号</button><button v-if="selected.order.pay_url" class="order-history-primary" @click="followOrder">{{ payment.canPay&&selected.group==='active'?'继续支付 →':'查看官方订单 ↗' }}</button><text v-else>官方未返回付款链接，可同步或在麦当劳 App 查看。</text></view>
          </template>
          <view v-else class="order-history-detail-placeholder"><view class="order-history-receipt-art"><text>McDonald’s</text><view></view><view></view><view></view><text>YOUR ORDER</text></view><text>点开一单，看看这次的美味。</text><text>门店、餐品、付款状态都在这里。</text></view>
        </view>
      </view>
      <view class="order-history-bottom"><text>状态以官方最新查询为准 · 时间均为北京时间</text><text>{{ asOf?'列表更新 '+time(asOf):'仅展示已保存记录' }}</text></view>
    </view>
  </view>
</template>

<style scoped>
.order-history-overlay{position:fixed;inset:0;z-index:2600;background:rgba(40,26,18,.45);backdrop-filter:blur(5px);display:flex;align-items:center;justify-content:center;padding:24px;box-sizing:border-box;color:#35251c}
.order-history-dialog{outline:none;width:940px;max-width:100%;height:min(84vh,810px);display:flex;flex-direction:column;background:#fffdf9;border:1px solid #eddcc9;border-radius:26px;overflow:hidden;box-shadow:0 28px 90px #38200c40}
.order-history-header{display:flex;justify-content:space-between;gap:18px;padding:24px 26px;background:linear-gradient(110deg,#fff5df,#fffaf4 65%,#fff0e7)}
.order-history-brand{display:flex;align-items:center;gap:15px;min-width:0}.order-history-logo{height:54px;width:54px;border-radius:16px;background:#da291c;display:flex;align-items:center;justify-content:center;flex-shrink:0}.order-history-logo image{width:39px;height:39px}
.order-history-eyebrow,.order-history-title,.order-history-subtitle,.order-history-store,.order-history-foods{display:block}.order-history-eyebrow{font-size:9px;letter-spacing:1.7px;color:#a56931;font-weight:800}.order-history-title{font-size:25px;font-weight:850;line-height:1.6;letter-spacing:-.5px}.order-history-subtitle{font-size:11px;color:#a0846d}
.order-history-close{border:1px solid #ead8c5;border-radius:50%;width:32px;height:32px;line-height:28px;padding:0;margin:0;background:#fff9;font-size:24px;color:#996e50;flex-shrink:0}button::after{border:0}button{cursor:pointer}button[disabled]{opacity:.45}
.order-history-toolbar{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:17px 25px 13px;border-bottom:1px solid #f0e6da}.order-history-tabs{display:flex;background:#f5eee5;border-radius:14px;padding:4px;gap:3px}.order-history-tabs button{margin:0;padding:8px 16px;border-radius:11px;font-size:13px;line-height:1.4;font-weight:750;background:none;color:#9b826d}.order-history-tabs button.active{background:#fff;color:#b1291d;box-shadow:0 2px 6px #3b1e0d0c}.order-history-tabs button text{font-size:10px;margin-left:5px;opacity:.65}
.order-history-sync,.order-history-refresh-one{padding:5px 0;margin:0;background:none;font-size:11px;line-height:1.5;color:#ac4b2e;white-space:nowrap}
.order-history-filters{display:flex;align-items:center;gap:7px;padding:12px 25px 0;flex-wrap:wrap}.order-history-filters button{padding:5px 12px;font-size:10px;line-height:1.5;background:#f4eee7;border-radius:16px;margin:0;color:#9c8470}.order-history-filters button.active{color:#a53022;background:#ffebe0}.order-history-filters>text{font-size:10px;color:#a28d7d}
.order-history-unknown{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:12px 25px 0;padding:9px 12px;background:#fff5de;border:1px solid #f2dfaf;border-radius:10px;font-size:11px;color:#94713d}.order-history-unknown button{padding:0;background:none;font-size:10px;line-height:1.5;color:#a34421;margin:0;flex-shrink:0}
.order-history-error,.order-history-note{padding:9px 25px;font-size:11px;line-height:1.6;background:#fff1eb;color:#b3432a}.order-history-error button{display:inline;padding:0;margin:0 0 0 8px;font-size:11px;line-height:1.6;background:none;color:#a52d21}.order-history-note{background:#faf6ee;color:#9a7d65}
.order-history-body{display:flex;flex:1;min-height:0;padding:18px 25px 15px;gap:18px}.order-history-list-panel{width:42%;min-width:0;display:flex;flex-direction:column}.order-history-list-heading{display:flex;justify-content:space-between;font-size:11px;color:#715540;padding-bottom:10px;font-weight:700}.order-history-list-heading text:last-child{font-size:10px;color:#b09986;font-weight:400}.order-history-list-scroll{height:100%;flex:1;min-height:0;padding-right:4px;box-sizing:border-box}
.order-history-item{border:1px solid #eee2d5;background:#fff;border-radius:15px;padding:14px;margin-bottom:10px;cursor:pointer;transition:border-color .15s,background .15s}.order-history-item.selected{border-color:#d89663;background:#fff7ed}.order-history-item-top{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:9px}.order-history-status{display:inline-block;padding:3px 8px;border-radius:7px;font-size:10px;font-weight:750;background:#eee7e0;color:#9b806c}.order-history-status.active{background:#fff0cc;color:#a45e14}.order-history-status.paid{background:#e7f2e9;color:#4a7c58}.order-history-status.unknown{background:#fff1dc;color:#9e6932}.order-history-status.inactive{background:#f0eeeb;color:#9d9387}.order-history-time{font-size:9px;color:#b2a08e}.order-history-store{font-size:13px;font-weight:750;line-height:1.6}.order-history-foods{font-size:11px;color:#a28b76;line-height:1.7;margin-top:4px;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}.order-history-item-bottom{display:flex;justify-content:space-between;align-items:center;margin-top:12px}.order-history-item-bottom text:first-child{font-size:17px;font-weight:800;color:#5d3c29}.order-history-item-bottom text:last-child{font-size:10px;color:#b85b34}
.order-history-empty{padding:45px 12px;text-align:center;display:flex;flex-direction:column;gap:10px;font-size:12px;color:#92755d}.order-history-empty>text:last-child{font-size:10px;color:#b39f8c}.order-history-empty-icon{font-size:36px;color:#d7b48a}.order-history-more{font-size:11px;color:#a96740;background:#f6eee3;border-radius:14px;line-height:1.5;padding:10px;margin:12px 0}
.order-history-detail-panel{flex:1;min-width:0;background:#fff;border:1px solid #eee0d0;border-radius:18px;display:flex;flex-direction:column;overflow:hidden}.order-history-detail-heading{display:flex;align-items:center;justify-content:space-between;padding:13px 17px;border-bottom:1px dashed #efdfce;font-size:11px;font-weight:700}.order-history-back{display:none}.order-history-detail-scroll{height:100%;min-height:0;flex:1}.order-history-detail-content{padding:20px}.order-history-detail-state{display:flex;flex-wrap:wrap;align-items:center;gap:9px}.order-history-detail-state>text:nth-child(2){font-size:9px;color:#b09a83}.order-history-detail-price{width:100%;font-size:31px;font-weight:850;color:#b93020;margin-top:4px}.order-history-detail-store{display:block;font-size:16px;font-weight:800;margin:15px 0 6px}.order-history-detail-muted{display:block;font-size:11px;line-height:1.8;color:#a48a74}.order-history-detail-meta{border-top:1px dashed #ebddcb;border-bottom:1px dashed #ebddcb;padding:12px 0;margin:16px 0}.order-history-detail-meta>view{display:flex;justify-content:space-between;gap:12px;padding:4px 0;font-size:10px;line-height:1.7}.order-history-detail-meta>view>text:first-child{color:#b09a86;flex-shrink:0}.order-history-detail-meta>view>text:last-child{text-align:right;overflow-wrap:anywhere;color:#8e725b}.order-history-number{max-width:75%}.order-history-section{font-size:11px;color:#8a6545;font-weight:800}.order-history-detail-food{display:flex;gap:14px;justify-content:space-between;padding:12px 0;border-bottom:1px solid #f2e9df;font-size:12px;line-height:1.7}.order-history-detail-food>text{white-space:nowrap;color:#b58b63}.order-history-detail-tip{display:block;margin-top:18px;border-radius:11px;padding:12px;background:#faf5ed;font-size:10px;line-height:1.8;color:#a58a70}
.order-history-detail-footer{display:flex;align-items:center;justify-content:flex-end;gap:8px;padding:13px 17px;border-top:1px solid #f0e3d4;flex-wrap:wrap}.order-history-detail-footer>text{font-size:10px;color:#a28b73;line-height:1.7}.order-history-primary,.order-history-outline{font-size:11px;font-weight:750;line-height:1.5;padding:9px 14px;border-radius:20px;margin:0;white-space:nowrap}.order-history-primary{background:#da291c;color:#fff}.order-history-outline{background:#fff;border:1px solid #ead7bf;color:#a3653c}
.order-history-detail-placeholder{display:flex;flex-direction:column;align-items:center;justify-content:center;flex:1;padding:28px;text-align:center;gap:9px;color:#a68b72;font-size:12px}.order-history-detail-placeholder>text:last-child{font-size:10px;color:#baa48f}.order-history-receipt-art{transform:rotate(-6deg);width:130px;border:1px solid #eddac0;border-radius:10px;background:#fffaf0;padding:20px 16px;margin-bottom:20px;box-shadow:7px 9px 0 #f7eee1}.order-history-receipt-art>text{display:block;color:#c3a175;font-size:10px;font-weight:700;letter-spacing:1px}.order-history-receipt-art>view{height:4px;background:#eee1cc;border-radius:4px;margin:13px 0}.order-history-receipt-art>view:nth-child(3){width:65%}.order-history-bottom{padding:10px 25px 14px;border-top:1px solid #efe3d5;display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;font-size:9px;color:#b09a84}
@media(max-width:700px){.order-history-overlay{padding:12px;align-items:flex-end}.order-history-dialog{height:90vh;border-radius:22px}.order-history-header{padding:18px 16px}.order-history-logo{height:43px;width:43px;border-radius:13px}.order-history-logo image{width:31px;height:31px}.order-history-brand{gap:11px}.order-history-title{font-size:19px}.order-history-eyebrow{font-size:7px;letter-spacing:1px}.order-history-subtitle{font-size:10px}.order-history-close{width:28px;height:28px;line-height:25px;font-size:21px}.order-history-toolbar{padding:13px 14px}.order-history-tabs button{font-size:12px;padding:8px 10px}.order-history-tabs button text{margin-left:3px}.order-history-sync{font-size:10px}.order-history-filters{padding:10px 14px 0}.order-history-unknown{margin:10px 14px 0;font-size:10px}.order-history-body{padding:14px;gap:0}.order-history-list-panel{width:100%}.order-history-detail-panel{display:none}.order-history-body.detail-open .order-history-list-panel{display:none}.order-history-body.detail-open .order-history-detail-panel{display:flex}.order-history-back{display:block;background:none;padding:0;font-size:10px;color:#a55c34;margin:0;line-height:1.7}.order-history-detail-content{padding:17px}.order-history-detail-heading{padding:12px 14px}.order-history-detail-heading>text{display:none}.order-history-detail-footer{padding:12px}.order-history-bottom{padding:10px 14px 12px;font-size:8px}.order-history-error,.order-history-note{padding:8px 14px}}
</style>
