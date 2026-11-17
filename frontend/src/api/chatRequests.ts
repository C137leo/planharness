/** Persist only claim identifiers, never message bodies, tokens or MCP headers. */
export type SavedChatRequest = { request_id: string; thread: string; started_at: number }

function key(owner: string) { return `planharness_chat_requests:${owner}` }

export function savedChatRequests(owner: string): SavedChatRequest[] {
  if (!owner) return []
  try {
    const rows: unknown = uni.getStorageSync(key(owner))
    return Array.isArray(rows) ? rows.filter((r): r is SavedChatRequest => Boolean(r &&
      typeof r.request_id === 'string' && r.request_id.length <= 128 &&
      typeof r.thread === 'string' && typeof r.started_at === 'number')).slice(-20) : []
  } catch { return [] }
}

export function rememberChatRequest(owner: string, requestId: string, thread: string): void {
  if (!owner) return
  try {
    const rows = savedChatRequests(owner).filter(r => r.request_id !== requestId)
    uni.setStorageSync(key(owner), [...rows, { request_id: requestId, thread, started_at: Date.now() }].slice(-20))
  } catch { /* Server claim remains authoritative if local storage is unavailable. */ }
}

export function lastSavedChatRequest(owner: string, thread?: string): SavedChatRequest | undefined {
  const rows = savedChatRequests(owner).filter(r => thread === undefined || r.thread === thread)
  return rows[rows.length - 1]
}

export function forgetChatRequest(owner: string, requestId: string): void {
  if (!owner) return
  try { uni.setStorageSync(key(owner), savedChatRequests(owner).filter(r => r.request_id !== requestId)) } catch { /* optional cache */ }
}
