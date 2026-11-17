/** 屏幕很矮时允许下限随屏幕收缩，确保关闭按钮和输入区仍在视口内。 */
export function workspaceHeightBounds(viewport: number) {
  const safeViewport = Number.isFinite(viewport) && viewport > 0 ? viewport : 800
  const max = Math.max(160, Math.floor(safeViewport - 24))
  return { min: Math.min(360, max), max }
}
export function clampWorkspaceHeight(value: unknown, viewport: number, fallback = 600) {
  const { min, max } = workspaceHeightBounds(viewport)
  const numeric = typeof value === 'number' && Number.isFinite(value) ? value : fallback
  return Math.round(Math.max(min, Math.min(max, numeric)))
}
