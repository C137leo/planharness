<script setup lang="ts">
import { ref,watch } from 'vue'
import { api,type WorkoutReplanState,type AgentActionChip } from '../api'
const props=defineProps<{value:WorkoutReplanState;actions:AgentActionChip[];busy:boolean}>()
const emit=defineEmits<{(e:'action',action:AgentActionChip):void}>()
const fresh=ref<WorkoutReplanState|null>(null),error=ref('');let generation=0
watch(()=>[props.value.authorization_id,props.value.etag],async()=>{
  const n=++generation;fresh.value=null;error.value=''
  try { const data=await api.getWorkoutReplan(props.value.authorization_id);if(n===generation)fresh.value=data }
  catch { if(n===generation)error.value='暂时无法核对重排确认，请稍后再试。' }
},{immediate:true})
function disabled(action:AgentActionChip){return props.busy || !fresh.value || fresh.value.etag!==props.value.etag || fresh.value.status!=='pending' || action.type==='confirm_tasks' && fresh.value.base_matches_current===false}
</script>
<template>
  <view class="replan-card">
    <text class="replan-kicker">重新安排之前</text>
    <text class="replan-title">{{ value.total_sets === 0 ? '确认制定今天的新训练目标？' : value.remaining_sets === 0 ? '保留已有记录，另行制定新目标？' : '保留已有记录，放弃旧方案未完成部分？' }}</text>
    <text class="replan-note">{{ value.total_sets === 0 ? '今天暂无有效训练安排。' : value.remaining_sets === 0 ? '当前已有记录已达到原目标。' : `旧方案还有 ${value.remaining_sets} 组未达到目标。` }}</text>
    <view class="replan-records"><text>当前系统已有记录：</text><text v-if="!value.recorded.length">尚无逐项完成记录</text><text v-for="(record,index) in value.recorded" :key="index">{{ record.name }} · 已记录 {{ record.completed_sets }} 组</text></view>
    <text class="replan-note">以上记录将单独保留。新方案进度从零开始，同名动作也不会复用旧完成组数。</text>
    <text class="replan-note">确认后先生成新目标预览；应用新方案前，原安排仍保留。若记录不符，可先在聊天中核对。</text>
    <text v-if="error" class="replan-warning">{{ error }}</text>
    <text v-if="fresh?.base_matches_current===false && fresh?.status==='pending'" class="replan-warning">记录或安排已变化，请重新核对后确认。</text>
    <text v-if="fresh && fresh.status!=='pending'" class="replan-note">{{ ({approved:'已确认进入新目标规划',applied:'新方案已应用，旧记录已保留',cancelled:'已取消重排，原安排保留',expired:'这次确认已过期，请重新发起重排确认。',stale:'安排或记录已变化，请核对最新状态后重新确认。'} as Record<string,string>)[fresh.status] || '本次确认已结束' }}</text>
    <view class="replan-actions"><button v-for="action in actions" :key="action.type" class="replan-button" :class="{primary:action.type==='confirm_tasks'}" :disabled="disabled(action)" @click="!disabled(action) && emit('action',action)">{{ action.type==='confirm_tasks' ? '保留记录，重新规划' : '继续原方案' }}</button></view>
  </view>
</template>
<style scoped>
.replan-card{padding:22px;border:1px solid #edceba;border-radius:20px;background:#fffaf5;margin:12px 0;color:#392b20}.replan-kicker,.replan-title,.replan-note,.replan-warning{display:block}.replan-kicker{font-size:12px;color:#aa5d39}.replan-title{font-size:20px;font-weight:700;margin:8px 0 12px}.replan-note{font-size:13px;color:#8d735f;line-height:1.7;margin-top:8px}.replan-records{display:flex;flex-direction:column;gap:8px;background:white;border-radius:12px;padding:15px;font-size:14px;margin:14px 0}.replan-actions{display:flex;justify-content:flex-end;gap:10px;flex-wrap:wrap;margin-top:18px}.replan-button{font-size:13px;border-radius:24px;line-height:40px;padding:0 16px;margin:0;border:1px solid #e7c9b3;background:white}.replan-button.primary{background:#ed5429;color:white}.replan-button[disabled]{opacity:.45}.replan-warning{font-size:13px;color:#b44e2b;margin-top:10px}
</style>
