export class ApiError extends Error {
  constructor(public code: string, public status: number) { super(code) }
}
export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api/v1${path}`, { ...options, headers: { 'Content-Type': 'application/json', ...options.headers } })
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new ApiError(body.error?.code || 'request_failed', response.status)
  }
  return response.status === 204 ? undefined as T : response.json()
}
export const errorText = (error: unknown) => {
  const code = error instanceof ApiError ? error.code : 'network_error'
  return ({ session_busy: '当前会话仍在执行，请稍后再试。', capacity_exceeded: '服务正在处理其他问题，请稍后重试。',
    storage_unavailable: '会话存储暂时不可用，请稍后重试。', not_found: '此会话或回答已不存在。',
    invalid_request: '请检查问题内容，问题最多 10,000 字。', idempotency_conflict: '提交内容发生变化，请重新发送。',
    network_error: '网络连接中断。再次发送会继续同一次提交。' } as Record<string, string>)[code] || '请求未完成，请稍后重试。'
}
