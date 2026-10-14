import type { Run, RunEvent, Section, Evidence } from './types'
export function applyEvent(run: Run, event: RunEvent) {
  if (event.run_id !== run.id || event.seq <= run.last_seq) return false
  run.last_seq = event.seq
  const data = event.data
  if (event.type === 'run.started') run.status = 'running'
  if (event.type.startsWith('retrieval.')) {
    run.activity = [...(run.activity || []), event]
  }
  if (event.type === 'answer.section_upsert') {
    const value = data as Section
    const i = run.sections.findIndex(s => s.section_id === value.section_id)
    if (i < 0) run.sections.push(value)
    else run.sections[i] = value
    run.sections.sort((a, b) => a.position - b.position)
    run.answer = run.sections.map(s => s.markdown).join('\n\n')
  }
  if (event.type === 'evidence.updated') {
    const items = data.items as Evidence[]
    run.evidence = data.replace ? items : [...new Map([...run.evidence, ...items].map(e => [e.label, e])).values()]
  }
  if (event.type === 'run.summary') run.summary = data
  if (event.type === 'run.finished') {
    run.status = data.status; run.reason = data.reason; run.error = data.error; run.answer = data.answer
  }
  return true
}
