import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'
import { flushPromises, mount } from '@vue/test-utils'
import ChatPage from '../src/ChatPage.vue'
import { useChat } from '../src/store'
import type { Run } from '../src/types'
const run = (): Run => ({ id: 'r1', session_id: 's1', query: 'q', profile: 'fast', status: 'running', reason: null, error: null, answer: '', last_seq: 0, worker_active: true, sections: [], evidence: [], summary: {} })

class Stream { close = vi.fn(); addEventListener = vi.fn(); onopen = null; onerror = null }
beforeEach(() => { setActivePinia(createPinia()); vi.stubGlobal('EventSource', Stream) })
afterEach(() => vi.unstubAllGlobals())
describe('chat interactions', () => {
  it('keeps IME composition and Shift+Enter from sending, Enter sends once', async () => {
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: ChatPage }] })
    await router.push('/'); await router.isReady()
    const chat = useChat()
    vi.spyOn(chat, 'list').mockResolvedValue()
    vi.spyOn(chat, 'select').mockResolvedValue()
    const send = vi.spyOn(chat, 'send').mockResolvedValue(false)
    const wrapper = mount(ChatPage, { global: { plugins: [router] } })
    await wrapper.get('textarea').setValue('问题')
    await wrapper.get('textarea').trigger('keydown', { key: 'Enter', isComposing: true })
    await wrapper.get('textarea').trigger('keydown', { key: 'Enter', shiftKey: true })
    expect(send).not.toHaveBeenCalled()
    await wrapper.get('textarea').trigger('keydown', { key: 'Enter' })
    expect(send).toHaveBeenCalledOnce()
    chat.sending = true
    await wrapper.get('textarea').trigger('keydown', { key: 'Enter' })
    expect(send).toHaveBeenCalledOnce()
    wrapper.unmount()
  })
  it('preserves the request id across a network retry', async () => {
    const chat = useChat()
    chat.current = { id: 's1', title: 't', updated_at: '', messages: [], runs: [] }
    const requests: any[] = []
    vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit) => {
      if (url.endsWith('/runs')) { requests.push(JSON.parse(init.body as string)); throw new Error('offline') }
      return { ok: true, json: async () => ({ items: [] }) }
    }))
    expect(await chat.send('q', 'fast')).toBe(false)
    expect(await chat.send('q', 'fast')).toBe(false)
    expect(requests[0].client_request_id).toBe(requests[1].client_request_id)
    expect(chat.error).toContain('网络')
  })
  it('restores a session snapshot and follows its unfinished run', async () => {
    const chat = useChat(), r = run()
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ id: 's1', title: 't', messages: [], runs: [r] }) })))
    await chat.select('s1')
    expect(chat.current?.runs[0].id).toBe('r1')
    expect(chat.busy).toBe(true)
    chat.disconnect()
  })
})
