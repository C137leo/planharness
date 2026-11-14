<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api, type DayRevision, type AgentActionChip } from '../api'
import { formatKcal } from '../styles/numbers'
const props=defineProps<{draft: DayRevision; actions: AgentActionChip[]; busy: boolean}>()
const emit=defineEmits<{(e:'action',action:AgentActionChip):void;(e:'ask',text:string):void}>()
const checked=ref(false), fresh=ref<DayRevision|null>(null), error=ref(''), unchangedOpen=ref(false)
let generation=0
watch(()=>[props.draft.revision_id,props.draft.etag],async()=>{
  const request=++generation;checked.value=false;error.value=''
  try { const value=await api.getDayRevision(props.draft.revision_id);if(request===generation){fresh.value=value;checked.value=true} }
  catch { if(request===generation)error.value='暂时无法核对草案状态，请稍后重新查看。' }
},{immediate:true})
const labels:Record<string,string>={awaiting_confirmation:'待你确认',applied:'已应用',cancelled:'已取消',conflicted:'数据已变化',expired:'已过期'}
const scopeLabels:Record<string,string>={workout:'训练',breakfast:'早餐',lunch:'午餐',dinner:'晚餐',snack:'加餐'}
const changed=computed(()=>props.draft.changes.filter(c=>c.kind!=='unchanged' && !(c.kind==='updated' && summary(c.before)===summary(c.after))))
const unchanged=computed(()=>props.draft.changes.filter(c=>c.kind==='unchanged' || c.kind==='updated' && summary(c.before)===summary(c.after)))
const stale=computed(()=>fresh.value && (fresh.value.etag!==props.draft.etag || fresh.value.base_matches_current===false && fresh.value.status==='awaiting_confirmation'))
function summary(value: Record<string,unknown>|string[]|null):string {
  if(!value)return '无'
  if(Array.isArray(value))return `${value.length} 项的展示顺序`
  if(value.items) return `${scopeLabels[String(value.slot)] || value.title || '餐次'}：` + ((value.items as Array<{name:string;grams:number}>).map(i=>`${i.name} ${i.grams}g`).join(' · ') || '未安排食物') + (typeof value.kcal==='number' ? ` · ${formatKcal(value.kcal)} kcal` : '')
  return `${value.name || value.action_ref || '训练动作'}${value.sets ? ` · ${value.sets}组` : ''}${value.reps ? `×${value.reps}` : ''}${value.duration_min ? ` · ${value.duration_min}分钟` : ''}`
}
function disabled(action:AgentActionChip) {
  return props.busy || !checked.value || fresh.value?.etag!==props.draft.etag || fresh.value?.status!=='awaiting_confirmation'
    || (action.type==='confirm_tasks' && !!stale.value)
}
</script>
<template>
  <view class="revision-card">
    <view class="revision-head"><view><text class="revision-kicker">当天计划 · 差异预览</text><text class="revision-title">{{ draft.date }} 的新安排</text></view><text class="revision-status">{{ labels[fresh?.status || draft.status] || draft.status }}</text></view>
    <text class="revision-scope">本次范围：{{ draft.scope.map(s=>scopeLabels[s] || s).join('、') }} · 第 {{ draft.version }} 版草案</text>
    <text v-if="draft.scope.includes('workout')" class="revision-note">训练时长：{{ draft.workout_minutes_before }} → {{ draft.workout_minutes_after }} 分钟（估计）</text>
    <text class="revision-note">确认后应用；已完成记录保留。未列入本次范围的内容保持原样。</text>
    <view v-if="draft.workout_policy==='replace_remaining'" class="revision-kept" style="margin-top:12px">
      <text>新方案进度从零开始；重排前已有记录单独保留：</text>
      <text v-for="(record,index) in draft.previous_training || []" :key="index">{{ record.name }} · 已记录 {{ record.completed_sets }} 组</text>
    </view>
    <view v-for="(change,index) in changed" :key="`${change.object_id}-${index}`" class="revision-change">
      <text class="revision-change-label">{{ ({added:'新增',removed:'移除',replaced:'替换',updated:'调整',order:'顺序调整'} as Record<string,string>)[change.kind] || change.kind }} · {{ change.domain==='workout' ? '训练' : '饮食' }}</text>
      <template v-if="change.kind!=='order'"><text class="revision-before">原：{{ summary(change.before) }}</text><text class="revision-after">新：{{ summary(change.after) }}</text></template>
      <text v-else class="revision-after">按预览中的顺序执行</text>
    </view>
    <text v-if="!changed.length" class="revision-note">内容与原安排相同。</text>
    <text v-if="unchanged.length" class="revision-toggle" @click="unchangedOpen=!unchangedOpen">{{ unchangedOpen ? '收起' : '查看' }} {{ unchanged.length }} 项保留内容</text>
    <view v-if="unchangedOpen" class="revision-kept"><text v-for="(change,index) in unchanged" :key="index">✓ {{ summary(change.after) }}</text></view>
    <text v-if="stale" class="revision-warning">计划、记录或草案版本已变化，请重新查看差异后再确认。</text>
    <text v-if="error" class="revision-warning">{{ error }}</text>
    <view class="revision-actions">
      <button v-for="action in actions" :key="action.type" class="revision-button" :class="{primary:action.type==='confirm_tasks'}" :disabled="disabled(action)" @click="!disabled(action) && emit('action',action)">{{ action.type==='confirm_tasks' ? '确认应用' : '取消草案' }}</button>
      <button v-if="stale || error" class="revision-button" :disabled="busy" @click="emit('ask','查看当前整日计划草案及最新差异')">重新查看</button>
    </view>
  </view>
</template>
<style scoped>
.revision-card{border:1px solid #edceba;background:linear-gradient(140deg,#fffaf5,#fff);border-radius:20px;padding:22px;margin:12px 0;color:#36251b}
.revision-head{display:flex;align-items:flex-start;justify-content:space-between;gap:14px}.revision-kicker,.revision-title,.revision-scope,.revision-note,.revision-before,.revision-after,.revision-warning{display:block}.revision-kicker{font-size:12px;color:#ac6440;letter-spacing:1px}.revision-title{font-size:22px;font-weight:700;margin-top:5px}.revision-status{font-size:12px;background:#fae9dd;border-radius:18px;padding:7px 11px;white-space:nowrap}.revision-scope{font-size:13px;color:#8c6d58;margin:12px 0 5px}.revision-note{font-size:13px;color:#927a67;line-height:1.6}.revision-change{padding:14px 0;border-bottom:1px solid #f2e2d5}.revision-change-label{font-size:13px;color:#b04e27;font-weight:600}.revision-before{color:#947f70;font-size:13px;margin-top:7px;line-height:1.6}.revision-after{color:#382820;font-size:14px;margin-top:4px;line-height:1.6}.revision-toggle{display:block;color:#aa532e;font-size:13px;margin:16px 0;cursor:pointer}.revision-kept{display:flex;flex-direction:column;gap:7px;font-size:13px;color:#877461}.revision-warning{font-size:13px;color:#ac4529;margin-top:14px}.revision-actions{display:flex;justify-content:flex-end;flex-wrap:wrap;gap:10px;margin-top:18px}.revision-button{font-size:14px;border-radius:25px;border:1px solid #e6c7b1;background:white;margin:0;padding:0 20px;line-height:40px}.revision-button.primary{background:#ee5227;color:#fff;border-color:#ee5227}.revision-button[disabled]{opacity:.45}
@media(max-width:600px){.revision-card{padding:15px}.revision-title{font-size:18px}.revision-head{gap:6px}.revision-button{padding:0 14px}}
</style>
