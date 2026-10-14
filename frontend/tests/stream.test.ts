import { describe, it, expect } from 'vitest'
import { applyEvent } from '../src/events'
import { renderMarkdown } from '../src/markdown'
import type { Run, RunEvent } from '../src/types'

function run(): Run {
  return { id: 'r', session_id: 's', query: 'q', profile: 'fast', status: 'running', reason: null, error: null,
    answer: '', last_seq: 0, worker_active: true, sections: [], evidence: [], summary: {}, drafts: [], protocol_version: 2 }
}
function event(seq: number, type: string, data: Record<string, any>): RunEvent {
  return { run_id: 'r', seq, type, data, timestamp: '' }
}
describe('incremental state', () => {
  it('replaces preview with a canonical section and ignores duplicates', () => {
    const value = run()
    expect(applyEvent(value, event(1, 'answer.generation_started', { generation_id: 'g' }))).toBe(true)
    expect(applyEvent(value, event(2, 'answer.section_started', { generation_id: 'g', section_id: 'S1', position: 0 }))).toBe(true)
    const delta = event(3, 'answer.text_delta', { generation_id: 'g', section_id: 'S1', delta: '草稿 [E9]', revision: 1 })
    expect(applyEvent(value, delta)).toBe(true)
    expect(applyEvent(value, delta)).toBe(false)
    expect(value.drafts![0].text).toBe('草稿 [E9]')
    expect(value.answer).toBe('')
    expect(applyEvent(value, event(4, 'answer.section_upsert', { generation_id: 'g', section_id: 'S1', revision: 2, position: 0, markdown: '正式正文', citations: [] }))).toBe(true)
    expect(value.answer).toBe('正式正文')
    expect(value.drafts).toEqual([])
  })
  it('does not advance its cursor for gaps, stale generations, or unknown events', () => {
    const value = run()
    applyEvent(value, event(1, 'answer.generation_started', { generation_id: 'g' }))
    expect(applyEvent(value, event(3, 'answer.section_started', { generation_id: 'g', section_id: 'S1' }))).toBe(false)
    expect(applyEvent(value, event(2, 'answer.section_started', { generation_id: 'old', section_id: 'S1' }))).toBe(false)
    expect(applyEvent(value, event(2, 'unknown', {}))).toBe(false)
    expect(value.last_seq).toBe(1)
  })
  it('resets a failed preview before retry and retains interrupted content', () => {
    const value = run()
    applyEvent(value, event(1, 'answer.generation_started', { generation_id: 'g' }))
    applyEvent(value, event(2, 'answer.section_started', { generation_id: 'g', section_id: 'S1', position: 0 }))
    applyEvent(value, event(3, 'answer.text_delta', { generation_id: 'g', section_id: 'S1', delta: '旧内容', revision: 1 }))
    applyEvent(value, event(4, 'answer.generation_started', { generation_id: 'g2' }))
    expect(value.drafts).toEqual([])
    applyEvent(value, event(5, 'answer.section_started', { generation_id: 'g2', section_id: 'S1', position: 0 }))
    applyEvent(value, event(6, 'answer.text_delta', { generation_id: 'g2', section_id: 'S1', delta: '新内容', revision: 1 }))
    applyEvent(value, event(7, 'answer.generation_finished', { generation_id: 'g2', status: 'failed' }))
    expect(value.drafts![0].state).toBe('interrupted')
    expect(value.drafts![0].text).toBe('新内容')
  })
  it('renders preview citations as text and only binds known canonical sources', () => {
    const preview = renderMarkdown('正文 [E1] <script>bad</script>\n```java\nint x;', { labels: ['E1'], preview: true })
    expect(preview).not.toContain('data-citation')
    expect(preview).not.toContain('<script>')
    expect(preview).toContain('<code')
    const canonical = renderMarkdown('正文 [E1] [E2]', { labels: ['E1'], preview: false })
    expect(canonical).toContain('data-citation="E1"')
    expect(canonical).not.toContain('data-citation="E2"')
  })
})
