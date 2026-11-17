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

/** 本地日历加天数，不用 UTC 或 24 小时毫秒，兼容跨月和夏令时。 */
export function upcomingWeek(start = todayStr()) {
  const [year, month, day] = start.split('-').map(Number)
  return Array.from({ length: 7 }, (_, offset) => {
    const value = new Date(year, month - 1, day + offset, 12)
    const weekday = isoWeekday(value)
    const date = `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}-${String(value.getDate()).padStart(2, '0')}`
    return { date, weekday, label: `${value.getMonth() + 1}/${value.getDate()}`, week: WEEKDAY_LABELS[weekday - 1], isToday: offset === 0 }
  })
}
