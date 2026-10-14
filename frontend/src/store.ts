import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { api, errorText } from './api'
import { applyEvent } from './events'
import { terminal, type Profile, type Run, type Session, type SessionDetail, type RunEvent } from './types'

export const useChat = defineStore('chat', () => {
  const sessions = ref<Session[]>([]), current = ref<SessionDetail | null>(null)
  const error = ref(''), connection = ref(''), sending = ref(false), hasMore = ref(false)
  const source = new Map<string, EventSource>(), timers = new Map<string, ReturnType<typeof setTimeout>>()
  let selection = 0
  let pending: { session: string; query: string; profile: Profile; id: string } | null = null
  const busy = computed(() => sending.value || !!current.value?.runs.some(r => r.worker_active))

  async function list(more = false) {
    try {
      const result = await api<{ items: Session[] }>(`/sessions?limit=50&offset=${more ? sessions.value.length : 0}`)
      sessions.value = more ? [...sessions.value, ...result.items] : result.items
      hasMore.value = result.items.length === 50
    } catch (e) { error.value = errorText(e) }
  }
  function disconnect() {
    source.forEach(s => s.close()); source.clear()
    timers.forEach(t => clearTimeout(t)); timers.clear()
    connection.value = ''
  }
  async function select(id?: string) {
    const ticket = ++selection
    disconnect(); current.value = null; error.value = ''
    if (!id) return
    try {
      const result = await api<SessionDetail>(`/sessions/${id}`)
      if (ticket !== selection) return
      current.value = result
      result.runs.filter(r => !terminal(r.status) || r.worker_active).forEach(follow)
    } catch (e) { if (ticket === selection) error.value = errorText(e) }
  }
  function update(run: Run) {
    if (current.value?.id !== run.session_id) return
    const i = current.value.runs.findIndex(r => r.id === run.id)
    if (i >= 0 && run.last_seq >= current.value.runs[i].last_seq) current.value.runs[i] = run
  }
  async function refresh(runId: string) {
    try {
      const run = await api<Run>(`/runs/${runId}`)
      update(run)
      error.value = ''
      if (terminal(run.status)) {
        source.get(runId)?.close(); source.delete(runId)
        if (!run.worker_active) { connection.value = ''; await list(); return }
      }
      if (current.value?.id === run.session_id && (connection.value || run.worker_active && terminal(run.status))) {
        timers.set(runId, setTimeout(() => { void refresh(runId) }, 2000))
      }
    } catch (e) {
      error.value = errorText(e)
      if (current.value?.runs.some(r => r.id === runId)) timers.set(runId, setTimeout(() => { void refresh(runId) }, 2000))
    }
  }
  function follow(run: Run) {
    if (terminal(run.status)) { if (run.worker_active) void refresh(run.id); return }
    source.get(run.id)?.close()
    const stream = new EventSource(`/api/v1/runs/${run.id}/events?after=${run.last_seq}`)
    source.set(run.id, stream)
    stream.onopen = () => { connection.value = '' }
    stream.onerror = () => { connection.value = '连接恢复中，回答会继续保存在会话中。'; void refresh(run.id) }
    const kinds = ['run.created', 'run.started', 'retrieval.action_completed', 'retrieval.context_built', 'evidence.updated', 'answer.section_upsert', 'run.summary', 'run.finished']
    for (const kind of kinds) stream.addEventListener(kind, (message) => {
      const event = JSON.parse((message as MessageEvent).data) as RunEvent
      const value = current.value?.runs.find(r => r.id === event.run_id)
      if (!value || !applyEvent(value, event)) return
      if (kind === 'run.finished') { stream.close(); source.delete(run.id); void refresh(run.id) }
    })
  }
  async function create() {
    const session = await api<Session>('/sessions', { method: 'POST', body: '{}' })
    await list(); await select(session.id)
    return session.id
  }
  async function send(query: string, profile: Profile) {
    if (!query.trim() || busy.value) return false
    error.value = ''; sending.value = true
    try {
      const id = current.value?.id || await create()
      if (!pending || pending.session !== id || pending.query !== query.trim() || pending.profile !== profile) {
        pending = { session: id, query: query.trim(), profile, id: crypto.randomUUID() }
      }
      const run = await api<Run>(`/sessions/${id}/runs`, { method: 'POST', body: JSON.stringify({ query: pending.query, profile, client_request_id: pending.id }) })
      pending = null
      if (current.value?.id === id) {
        const detail = await api<SessionDetail>(`/sessions/${id}`)
        if (current.value?.id === id) { current.value = detail; detail.runs.filter(r => !terminal(r.status) || r.worker_active).forEach(follow) }
      }
      await list()
      return !!run
    } catch (e) {
      error.value = errorText(e)
      // A lost POST response may still have created a run. Discover it without submitting again.
      if (pending && current.value?.id === pending.session) {
        try {
          const detail = await api<SessionDetail>(`/sessions/${pending.session}`)
          if (current.value?.id === detail.id) {
            current.value = detail
            detail.runs.filter(r => !terminal(r.status) || r.worker_active).forEach(follow)
          }
        } catch { /* Keep the original error and request id for an explicit retry. */ }
      }
      return false
    }
    finally { sending.value = false }
  }
  async function cancel(run: Run) {
    try { update(await api<Run>(`/runs/${run.id}/cancel`, { method: 'POST' })); void refresh(run.id) }
    catch (e) { error.value = errorText(e) }
  }
  async function rename(id: string, title: string) {
    try { await api(`/sessions/${id}`, { method: 'PATCH', body: JSON.stringify({ title }) }); await list(); if (current.value?.id === id) current.value.title = title }
    catch (e) { error.value = errorText(e) }
  }
  async function remove(id: string) {
    try { await api(`/sessions/${id}`, { method: 'DELETE' }); if (current.value?.id === id) await select(); await list(); return true }
    catch (e) { error.value = errorText(e); return false }
  }
  return { sessions, current, error, connection, sending, busy, hasMore, list, select, create, send, cancel, rename, remove, disconnect }
})
