/** Format at presentation boundaries; keep business calculations numeric. */
export function formatKcal(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  const number = Number(value)
  return Number.isFinite(number) ? number.toFixed(2) : '—'
}
