/** Account identity for local cache keys; never used as server authorization. */
export const AUTH_TOKEN_KEY = 'planharness_token'
export function decodeAccountId(auth: string): string {
  if (!auth) return ''
  try {
    const raw = auth.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')
    const payload = JSON.parse(globalThis.atob(raw.padEnd(Math.ceil(raw.length / 4) * 4, '='))) as {sub?: string}
    return typeof payload.sub === 'string' ? payload.sub : ''
  } catch { return '' }
}
export function currentAccountId(): string {
  return decodeAccountId((uni.getStorageSync(AUTH_TOKEN_KEY) as string) || '')
}
