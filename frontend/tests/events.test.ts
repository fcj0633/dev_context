import { describe, expect, it } from 'vitest'
import { applyEvent } from '../src/events'
import { renderMarkdown } from '../src/markdown'
import type { Run, RunEvent } from '../src/types'

export const run = (): Run => ({ id: 'r1', session_id: 's1', query: 'q', profile: 'fast', status: 'running', reason: null, error: null, answer: '', last_seq: 0, worker_active: true, sections: [], evidence: [], summary: {} })
describe('durable event reducer', () => {
  it('upserts sections in order and ignores duplicate, old and other-run events', () => {
    const r = run()
    const event: RunEvent = { run_id: 'r1', seq: 1, type: 'answer.section_upsert', timestamp: '', data: { section_id: 'S1', markdown: 'one', citations: ['E1'], position: 0 } }
    expect(applyEvent(r, event)).toBe(true)
    expect(applyEvent(r, event)).toBe(false)
    applyEvent(r, { ...event, seq: 2, data: { ...event.data, markdown: 'updated' } })
    expect(r.sections).toHaveLength(1)
    expect(r.answer).toBe('updated')
    expect(applyEvent(r, { ...event, run_id: 'other', seq: 3 })).toBe(false)
    applyEvent(r, { ...event, seq: 3, type: 'run.finished', data: { status: 'partial', reason: 'evidence_insufficient', error: null, answer: 'final' } })
    expect(r.answer).toBe('final'); expect(r.status).toBe('partial')
  })
  it('replaces evidence snapshots without inventing references', () => {
    const r = run()
    applyEvent(r, { run_id: 'r1', seq: 1, type: 'evidence.updated', timestamp: '', data: { items: [{ label: 'E1', chunk_id: 7 }], replace: true } })
    applyEvent(r, { run_id: 'r1', seq: 2, type: 'evidence.updated', timestamp: '', data: { items: [], replace: true } })
    expect(r.evidence).toEqual([])
  })
})
describe('safe Markdown', () => {
  it('escapes HTML, refuses JavaScript links and makes evidence clickable', () => {
    const output = renderMarkdown('<script>alert(1)</script> [bad](javascript:alert(1)) [E1]')
    expect(output).not.toContain('<script>')
    expect(output).not.toContain('href="javascript:')
    expect(output).toContain('data-citation="E1"')
  })
  it('highlights Java and SQL and keeps code citations literal', () => {
    const output = renderMarkdown('```java\npublic void order() {} // [E1]\n```\n```sql\nSELECT * FROM orders;\n```')
    expect(output).toContain('hljs-keyword')
    expect(output).toContain('复制代码')
    expect(output).not.toContain('data-citation=')
  })
})
