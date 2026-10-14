import type { Run, RunEvent, Section, Evidence } from './types'
export function applyEvent(run: Run, event: RunEvent) {
  if (event.run_id !== run.id || event.seq <= run.last_seq) return false
  if (run.protocol_version === 2 && event.seq !== run.last_seq + 1) return false
  if (!['run.created', 'run.started', 'evidence.updated', 'answer.generation_started', 'answer.section_started', 'answer.text_delta', 'answer.section_upsert', 'answer.generation_finished', 'run.summary', 'run.finished'].includes(event.type) && !event.type.startsWith('retrieval.')) return false
  const data = event.data
  if (event.type.startsWith('answer.') && event.type !== 'answer.generation_started' && data.generation_id && data.generation_id !== run.active_generation_id) return false
  if (event.type === 'answer.section_started' && run.drafts?.some(d => d.generation_id === data.generation_id && d.section_id === data.section_id)) return false
  if (event.type === 'answer.text_delta' || event.type === 'answer.section_upsert' && data.generation_id) {
    const draft = run.drafts?.find(d => d.generation_id === data.generation_id && d.section_id === data.section_id)
    if (!draft || draft.state !== 'streaming' || data.revision !== draft.revision + 1) return false
  }
  run.last_seq = event.seq
  if (event.type === 'answer.generation_started') { run.drafts = []; run.active_generation_id = data.generation_id }
  if (event.type === 'answer.section_started') {
    run.drafts ||= []
    run.drafts.push({ generation_id: data.generation_id, section_id: data.section_id, position: data.position, revision: 0, state: 'streaming', text: '' })
  }
  if (event.type === 'answer.text_delta') {
    const draft = run.drafts!.find(d => d.generation_id === data.generation_id && d.section_id === data.section_id)!
    draft.text += data.delta; draft.revision = data.revision
  }
  if (event.type === 'answer.generation_finished') {
    run.drafts?.filter(d => d.generation_id === data.generation_id).forEach(d => { d.state = 'interrupted' })
  }
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
    run.drafts = run.drafts?.filter(d => !(d.section_id === value.section_id && d.generation_id === data.generation_id))
  }
  if (event.type === 'evidence.updated') {
    const items = data.items as Evidence[]
    run.evidence = data.replace ? items : [...new Map([...run.evidence, ...items].map(e => [e.label, e])).values()]
  }
  if (event.type === 'run.summary') run.summary = data
  if (event.type === 'run.finished') {
    run.status = data.status; run.reason = data.reason; run.error = data.error; run.answer = data.answer
    if (data.drafts) run.drafts = data.drafts
  }
  return true
}
