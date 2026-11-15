import type { AgentChatResult } from '../api/types'

/** Official receipt deadlines without offsets are China local time. */
export function paymentPresentation(order: AgentChatResult['order'], now: number) {
  const text=(order?.payment_deadline || order?.expire_at || '').replace(' ','T')
  const deadline=text ? Date.parse(/[Zz]|[+-]\d\d:\d\d$/.test(text) ? text : text+'+08:00') : NaN
  const paid=order?.lifecycle==='paid' || ['2','4','6','8','10'].includes(order?.status_code || '') || ['已支付','已完成','已评价','配送中','餐厅配餐中'].some(s=>order?.status?.includes(s))
  const cancelled=order?.lifecycle==='cancelled' || order?.status_code==='7' || order?.status==='已取消'
  const expired=!paid && !cancelled && (order?.lifecycle==='expired' || Number.isFinite(deadline) && deadline<=now)
  const closed=paid || cancelled
  const canPay=!expired && !closed && Boolean(order?.pay_url)
  const label=expired || closed ? '查看官方订单 →' : order?.verified===false ? '前往官方核对并付款 →' : '去麦当劳付款 →'
  return {expired,closed,label,canPay,paid,cancelled}
}


/** A day snapshot accompanies all replies; it is not evidence of a mutation. */
export function isCommerceResult(result: AgentChatResult | null | undefined): boolean {
  if (!result) return false
  return Boolean(result.commerce_availability || result.checkout || result.commerce_catalog || result.commerce_connection || result.commerce_nutrition || result.mcd_order ||
    [...(result.task_plan || []),...(result.task_results || []),...(result.pending_confirmation?.tasks || [])]
      .some(t=>String(t.capability_id || '').startsWith('commerce.')))
}

export function dayAttachment(result: AgentChatResult) {
  if (!result.day || isCommerceResult(result)) return undefined
  const changed=(result.task_results || []).some(t=>t.status==='success' && t.actual_changes?.some(c=>['actions','meals'].includes(c.kind) && (!result.day?.date || !c.date || c.date===result.day.date)))
  if (changed) return {kind:'今日调整',title:result.day.summary || '今天的临时安排',result}
  const inspected=(result.task_plan || []).some(t=>t.capability_id==='context.inspect')
  return inspected ? {kind:'今日安排',title:'今天的当前安排',result} : undefined
}
