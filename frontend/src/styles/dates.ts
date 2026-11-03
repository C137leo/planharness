/** 日期小工具：只用 YYYY-MM-DD 字符串，避免时区把"今天"算错。 */

export function todayStr(): string {
  const now = new Date()
  const month = `${now.getMonth() + 1}`.padStart(2, '0')
  const day = `${now.getDate()}`.padStart(2, '0')
  return `${now.getFullYear()}-${month}-${day}`
}

/** 1=周一 … 7=周日，和计划里的 day_index 同一套编号。 */
export function isoWeekday(date = new Date()): number {
  const day = date.getDay()
  return day === 0 ? 7 : day
}

export const WEEKDAY_LABELS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
