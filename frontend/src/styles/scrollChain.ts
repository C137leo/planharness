/** Hand off a vertical wheel at a list boundary, including fractional pixels. */
export function forwardBoundaryWheel(element: HTMLElement, event: WheelEvent): boolean {
  if (event.defaultPrevented || event.ctrlKey || event.metaKey || !event.deltaY || Math.abs(event.deltaX) > Math.abs(event.deltaY)) return false
  const down = event.deltaY > 0
  const remaining = down ? element.scrollHeight - element.clientHeight - element.scrollTop : element.scrollTop
  if (remaining > 1) return false
  const doc = element.ownerDocument
  const page = doc.scrollingElement as HTMLElement | null
  const canMove = (target: HTMLElement) => down
    ? target.scrollHeight - target.clientHeight - target.scrollTop > 1
    : target.scrollTop > 1
  let parent = element.parentElement
  while (parent && parent !== page && parent !== doc.body) {
    const overflow = doc.defaultView?.getComputedStyle(parent).overflowY
    if ((overflow === 'auto' || overflow === 'scroll') && canMove(parent)) break
    // Respect a containing modal's explicit scroll isolation.
    if (doc.defaultView?.getComputedStyle(parent).overscrollBehaviorY === 'contain') return false
    parent = parent.parentElement
  }
  const target = parent && parent !== doc.body && parent !== page ? parent : page
  if (!target || !canMove(target)) return false
  event.preventDefault()
  const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? element.clientHeight : 1
  target.scrollTop += event.deltaY * unit
  return true
}
