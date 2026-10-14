export type Status = 'queued' | 'running' | 'completed' | 'partial' | 'failed' | 'cancelled'
export type Profile = 'fast' | 'full'
export interface Section { section_id: string; markdown: string; citations: string[]; position: number }
export interface Evidence { label: string; chunk_id: number; citation: { file_path: string; start_line: number | null; end_line: number | null; class_name?: string; symbol_name?: string; source_type: string }; content: string; truncated: boolean }
export interface Summary { evidence_state?: string; generation_status?: string; stop_reason?: string; retry_count?: number; stages?: { stage: string; latency_ms: number }[]; steps?: { step: number; tools: string[]; new_evidence_count: number; latency_ms: number }[]; coverage?: { requirement_id: string; state: string }[] }
export interface Run { id: string; session_id: string; query: string; profile: Profile; status: Status; reason: string | null; error: { code: string; message: string } | null; answer: string; last_seq: number; worker_active: boolean; sections: Section[]; evidence: Evidence[]; summary: Summary; activity?: RunEvent[] }
export interface Session { id: string; title: string; updated_at: string }
export interface Message { id: string; role: 'user' | 'assistant'; content: string; run_id: string }
export interface SessionDetail extends Session { messages: Message[]; runs: Run[] }
export interface RunEvent { run_id: string; seq: number; type: string; timestamp: string; data: Record<string, any> }
export const terminal = (status: Status) => ['completed', 'partial', 'failed', 'cancelled'].includes(status)
