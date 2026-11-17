<script setup lang="ts">
import { computed, ref, watch, onMounted, onUnmounted } from 'vue'
import { upcomingWeek, todayStr } from '../styles/dates'
import StableScroll from './StableScroll.vue'
import { actionLabel } from '../styles/copy'
import { mealThumb } from '../styles/thumbs'
import type { PlanDraft } from '../api'
const props = defineProps<{ draft: PlanDraft }>()
const expanded = ref(true)
watch(() => props.draft.draft_id, () => { expanded.value = true })
const start = ref(todayStr())
let dateTimer: ReturnType<typeof setInterval> | undefined
onMounted(() => { dateTimer = setInterval(() => { start.value = todayStr() }, 60000) })
onUnmounted(() => { if (dateTimer) clearInterval(dateTimer) })
const days = computed(() => upcomingWeek(start.value))
const trainingDays = computed(() => days.value.map(date => ({ ...date,
  actions: (props.draft.days || []).find(day => day.day_index === date.weekday)?.actions || [],
})))
const visibleDays = computed(() => expanded.value ? days.value : days.value.slice(0,3))
const meals = (n: number) => (props.draft.meals || []).filter(m => m.day_index === n)
const slots: Record<string,string> = {breakfast:'早餐',lunch:'午餐',dinner:'晚餐',snack:'加餐'}
</script>
<template>
  <view class="preview-section-head"><text>一周训练</text><text>训练日之间安排恢复</text></view>
  <StableScroll scroll-y chain-to-parent class="preview-scroll" aria-label="训练草案">
    <view class="preview-grid">
      <view v-for="day in trainingDays" :key="day.date" class="preview-day">
        <view class="preview-day-head"><text>{{ day.label }} · {{ day.week }}</text><text>{{ day.actions.reduce((n,a) => n + (a.duration_min || 0),0) }} 分钟</text></view>
        <text v-if="!day.actions.length" class="preview-meta">恢复日 · 无训练安排</text>
        <view v-for="(action,index) in day.actions" :key="index" class="preview-action"><text class="preview-number">{{ index + 1 }}</text><view><text class="preview-name">{{ actionLabel(action) }}</text><text class="preview-meta">{{ action.sets || 1 }} 组<template v-if="action.reps"> × {{ action.reps }} 次</template> · {{ action.duration_min || '—' }} 分钟</text></view></view>
      </view>
    </view>
  </StableScroll>
  <view class="preview-section-head"><text>饮食安排</text><text>{{ expanded ? '整周' : '先看3天' }} · 每天三餐</text></view>
  <text class="preview-meta">图片仅作餐次示意，食材与份量以文字为准。</text>
  <StableScroll scroll-y chain-to-parent class="preview-scroll meals" aria-label="饮食草案">
    <view class="preview-grid">
      <view v-for="day in visibleDays" :key="day.date" class="preview-day">
        <view class="preview-day-head"><text>{{ day.label }} · {{ day.week }}</text><text>{{ Math.round(meals(day.weekday).reduce((n,m) => n + (m.kcal || 0),0)) }} 千卡</text></view>
        <text v-if="!meals(day.weekday).length" class="preview-meta">暂无饮食安排</text>
        <view v-for="meal in meals(day.weekday)" :key="meal.slot" class="preview-meal"><image class="preview-meal-image" :src="mealThumb(meal.slot)" mode="aspectFill" aria-label="餐次分类示意图"/><text class="preview-name">{{ slots[meal.slot] || meal.title }}</text><text class="preview-meta">{{ (meal.items || []).map(i => `${i.name} ${i.grams}克`).join(' · ') }}</text></view>
      </view>
    </view>
  </StableScroll>
  <button v-if="days.length > 3" class="preview-expand" @click="expanded = !expanded">{{ expanded ? '收起为3天' : `展开其余 ${days.length - 3} 天饮食` }}</button>
</template>
<style scoped>
.preview-meal-image{width:48px;height:48px;border-radius:10px;float:right;margin-left:8px}.preview-meal::after{content:'';display:block;clear:both}
.preview-section-head{display:flex;justify-content:space-between;gap:12px;margin:20px 0 12px;font-size:14px;font-weight:650;color:#402a20}.preview-section-head text:last-child{font-size:12px;font-weight:400;color:#927e70}.preview-scroll{max-height:320px}.preview-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,190px),1fr));gap:12px;padding:2px}.preview-day{background:#fffaf5;border:1px solid #f0dfd0;border-radius:16px;padding:14px}.preview-day-head{display:flex;justify-content:space-between;gap:8px;padding-bottom:10px;color:#b84c25;font-size:13px;font-weight:650;border-bottom:1px solid #eddfd2}.preview-day-head text:last-child{font-size:11px;font-weight:400;color:#927e70}.preview-action{display:flex;align-items:flex-start;gap:9px;padding:11px 0 0}.preview-number{border-radius:8px;background:#f8e8db;color:#ac5d35;width:23px;line-height:23px;text-align:center;font-size:11px;flex-shrink:0}.preview-name{display:block;font-size:13px;line-height:1.5;color:#3e2c23;font-weight:600}.preview-meta{display:block;font-size:11px;line-height:1.7;color:#8c796b;margin-top:2px}.preview-meal{padding:10px 0;border-bottom:1px dashed #ecdfd3}.preview-meal:last-child{border:0;padding-bottom:0}.preview-expand{margin:12px auto 0;background:#fff4e9;border:1px solid #ead5c1;color:#ae572d;border-radius:22px;font-size:12px;line-height:34px;padding:0 18px}.preview-expand::after{border:0}@media(max-width:600px){.preview-grid{grid-template-columns:repeat(auto-fit,minmax(min(100%,190px),1fr))}.preview-scroll{max-height:330px}}
</style>
