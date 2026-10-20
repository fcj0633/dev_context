<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowUp, BookOpen, Check, ChevronDown, Code2, FileCode2, Menu, MessageSquare, MoreHorizontal, PanelLeftClose, Plus, Search, Square, X } from '@lucide/vue'
import { useChat } from './store'
import AnswerBody from './AnswerBody.vue'
import { errorText } from './api'
import type { Evidence, Profile, Run } from './types'

const chat = useChat(), route = useRoute(), router = useRouter()
const draft = ref(''), profile = ref<Profile>('fast'), sidebar = ref(false), query = ref('')
const viewport = ref<HTMLElement>(), input = ref<HTMLTextAreaElement>(), autoScroll = ref(true)
const selectedEvidence = ref<{ evidence?: Evidence; label: string } | null>(null)
const renameId = ref(''), renameTitle = ref(''), deleteId = ref(''), actionId = ref('')
const copied = ref('')
let inputObserver: ResizeObserver | undefined
let inputWidth = 0
function resizeInput() {
  const node = input.value
  if (!node) return
  node.style.height = '24px'
  const contentHeight = node.value ? node.scrollHeight : 24
  node.style.height = `${Math.max(24, Math.min(72, contentHeight))}px`
  node.style.overflowY = contentHeight > 72 ? 'auto' : 'hidden'
}
watch(draft, resizeInput, { flush: 'post' })
let previousFocus: HTMLElement | null = null
watch(() => !!selectedEvidence.value || !!renameId.value || !!deleteId.value, async open => {
  if (open) {
    previousFocus = document.activeElement as HTMLElement
    await nextTick()
    document.querySelector<HTMLElement>('[role="dialog"] button, [role="dialog"] input')?.focus()
  } else previousFocus?.focus()
})
function dialogKey(event: KeyboardEvent) {
  if (!selectedEvidence.value && !renameId.value && !deleteId.value) return
  if (event.key === 'Escape') { selectedEvidence.value = null; renameId.value = ''; deleteId.value = ''; return }
  if (event.key !== 'Tab') return
  const elements = [...document.querySelectorAll<HTMLElement>('[role="dialog"] button:not(:disabled), [role="dialog"] input')]
  const first = elements[0], last = elements[elements.length - 1]
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
}
const visibleSessions = computed(() => chat.sessions.filter(s => s.title.toLowerCase().includes(query.value.toLowerCase())))
const active = computed(() => chat.current?.runs.find(r => r.worker_active))
const runs = computed(() => chat.current?.runs || [])
const statusText = (run: Run) => ({ queued: '等待执行', running: '生成中', completed: '已完成', partial: '部分回答', failed: '执行失败', cancelled: run.worker_active ? '正在停止' : '已停止' })[run.status]
const reasonText = (run: Run) => run.error?.message || ({ evidence_insufficient: '核心证据尚不完整。请结合下方证据与覆盖状态阅读。', service_interrupted: '服务中断，已保留此前内容；未完成部分已标注。', user_cancelled: '已停止生成，保留停止前的内容。' } as Record<string, string>)[run.reason || ''] || ''
const stageName = (name: string) => ({ evidence_planning: '证据规划', agent_planning: '工具规划', evidence_retrieval: '检索取证', coverage_check: '覆盖检查', teaching_planning_v3: '教学规划', explanation_planning: '教学规划', teaching_evidence_pack: '证据组织', teaching_draft: '回答生成', teaching_fallback_draft: '回答恢复' } as Record<string, string>)[name] || name

watch(() => route.params.id, async id => {
  sidebar.value = false; selectedEvidence.value = null
  // Persisting a freshly created session URL must not tear down its live stream.
  if (chat.current?.id !== id) await chat.select(id as string | undefined)
}, { immediate: true })
watch(() => runs.value.map(r => r.answer + (r.drafts || []).map(d => d.text).join('')).join(''), async () => { await nextTick(); if (autoScroll.value && viewport.value) viewport.value.scrollTop = viewport.value.scrollHeight })
onMounted(() => {
  void chat.list(); input.value?.focus(); resizeInput()
  if (typeof ResizeObserver !== 'undefined' && input.value) {
    inputObserver = new ResizeObserver(entries => {
      const width = entries[0]?.contentRect.width || 0
      if (Math.abs(width - inputWidth) < .5) return
      inputWidth = width
      resizeInput()
    })
    inputObserver.observe(input.value)
  } else window.addEventListener('resize', resizeInput)
  document.addEventListener('keydown', dialogKey)
})
onUnmounted(() => {
  chat.disconnect(); inputObserver?.disconnect()
  window.removeEventListener('resize', resizeInput)
  document.removeEventListener('keydown', dialogKey)
})

async function send() {
  const ok = await chat.send(draft.value, profile.value)
  if (ok) {
    draft.value = ''; autoScroll.value = true
    if (chat.current && route.params.id !== chat.current.id) await router.replace(`/sessions/${chat.current.id}`)
    await nextTick(); input.value?.focus()
  }
}
function keydown(event: KeyboardEvent) {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault(); if (!chat.busy) void send()
  }
}
function onScroll() {
  const node = viewport.value
  if (node) autoScroll.value = node.scrollHeight - node.scrollTop - node.clientHeight < 90
}
async function newChat() { draft.value = ''; await router.push('/'); await chat.select(); input.value?.focus() }
async function markdownClick(event: MouseEvent, run: Run) {
  const target = (event.target as HTMLElement).closest<HTMLElement>('button')
  if (target?.dataset.citation) { event.preventDefault(); openEvidence(run, target.dataset.citation); return }
  if (target?.classList.contains('copy-code')) {
    const text = target.parentElement?.querySelector('code')?.textContent || ''
    try { await navigator.clipboard.writeText(text); target.textContent = '已复制'; setTimeout(() => { target.textContent = '复制代码' }, 1500) }
    catch { chat.error = '无法复制，请选择代码后手动复制。' }
  }
}
function openEvidence(run: Run, label: string) { selectedEvidence.value = { label, evidence: run.evidence.find(e => e.label === label) } }
async function copyAnswer(run: Run) {
  try { const tail = (run.drafts || []).filter(d => d.text && d.state !== 'committed' && d.state !== 'superseded').map(d => d.text).join('\n\n')
    await navigator.clipboard.writeText(run.answer + (tail ? '\n\n[以下内容尚未完成]\n' + tail : '')); copied.value = run.id; setTimeout(() => { copied.value = '' }, 1500) }
  catch { chat.error = '无法复制，请选择回答后手动复制。' }
}
async function confirmRename() { await chat.rename(renameId.value, renameTitle.value); renameId.value = '' }
async function confirmDelete() { const id = deleteId.value; if (await chat.remove(id)) { deleteId.value = ''; if (route.params.id === id) await router.replace('/') } }
async function retry(run: Run) { draft.value = run.query; profile.value = run.profile; await send() }
</script>

<template>
  <div class="app-shell">
    <div v-if="sidebar" class="mobile-scrim" @click="sidebar = false"></div>
    <aside class="sidebar" :class="{ open: sidebar }" aria-label="历史会话">
      <a class="brand" href="/" @click.prevent="newChat"><span class="brand-mark"><Code2 :size="23" /></span><span>DevContext<span class="brand-caption">代码有据，理解有路</span></span></a>
      <button class="new-chat" @click="newChat"><Plus :size="18" /> 新建会话 <span class="shortcut">新问题</span></button>
      <label class="history-search"><Search :size="16" /><input v-model="query" placeholder="搜索会话" aria-label="搜索会话" /></label>
      <div class="history-heading">最近的学习记录</div>
      <nav class="history-list">
        <div v-for="session in visibleSessions" :key="session.id" class="history-item" :class="{ selected: chat.current?.id === session.id }">
          <RouterLink :to="`/sessions/${session.id}`"><MessageSquare :size="16" /><span>{{ session.title }}</span></RouterLink>
          <button class="icon-button session-menu" :aria-label="`管理会话 ${session.title}`" @click="actionId = actionId === session.id ? '' : session.id"><MoreHorizontal :size="17" /></button>
          <div v-if="actionId === session.id" class="session-actions"><button @click="renameId = session.id; renameTitle = session.title; actionId = ''">重命名</button><button @click="deleteId = session.id; actionId = ''">删除</button></div>
        </div>
        <p v-if="!visibleSessions.length" class="history-empty">{{ query ? '没有匹配的会话' : '开始提问，学习记录会保存在这里。' }}</p>
        <button v-if="chat.hasMore" class="text-button" @click="chat.list(true)">加载更多</button>
      </nav>
      <div class="sidebar-footer"><span class="local-dot"></span><span>本地知识库<span class="subtle">Java · SQL · 项目文档</span></span><span class="version">V1</span></div>
    </aside>

    <main class="chat-main">
      <header class="topbar"><button class="icon-button mobile-menu" aria-label="打开历史会话" @click="sidebar = true"><Menu :size="21" /></button><div class="page-title">{{ chat.current?.title || '新的探索' }}</div><span class="grounded-badge"><BookOpen :size="14" /> 基于真实项目证据</span></header>
      <div ref="viewport" class="conversation" @scroll="onScroll">
        <section v-if="!runs.length" class="welcome">
          <div class="welcome-symbol"><Code2 :size="31" /></div><div class="eyebrow">YOUR CODE. CLEARER.</div>
          <h1>从代码出发，<br />把设计想明白。</h1><p>问实现、聊原理、追踪调用关系。<br />每一次解释，都可以回到真实的代码证据。</p>
          <div class="suggestions"><button @click="draft = '购票流程如何保证库存与订单的一致性？'; input?.focus()"><FileCode2 :size="18" /><strong>理解实现</strong><span>购票的一致性是如何保证的？</span></button><button @click="draft = '为什么余票查询使用令牌桶？'; input?.focus()"><BookOpen :size="18" /><strong>弄懂设计</strong><span>为什么余票查询使用令牌桶？</span></button></div>
        </section>
        <div v-else class="thread">
          <article v-for="run in runs" :key="run.id" class="exchange">
            <div class="user-question"><span class="role-label">你</span><div>{{ run.query }}</div></div>
            <div class="assistant-answer"><div class="answer-heading"><span class="mini-mark"><Code2 :size="18" /></span><strong>DevContext</strong><span class="mode-label">{{ run.profile === 'fast' ? 'Fast' : 'Full' }}</span><span class="run-status" :class="run.status"><span v-if="['queued','running'].includes(run.status)" class="running-dot"></span>{{ statusText(run) }}</span></div>
              <div v-if="run.answer || run.drafts?.some(d => d.text)" @click="markdownClick($event, run)"><AnswerBody :run="run" /></div>
              <div v-else-if="['queued', 'running'].includes(run.status)" class="working"><span class="pulse-dots"><i></i><i></i><i></i></span>{{ run.active_generation_id ? '正在生成正文…' : '正在查找证据并组织回答…' }}</div>
              <p v-if="reasonText(run)" class="status-notice" :class="run.status" role="status">{{ reasonText(run) }}</p>
              <details class="execution"><summary><span><ChevronDown :size="15" /> Agent 执行记录</span><span>{{ run.activity?.filter(a => a.type === 'retrieval.action_completed').length || 0 }} 次检索 · {{ run.evidence.length }} 条证据</span></summary>
                <ul class="activity"><li v-for="event in run.activity" :key="event.seq"><span class="activity-dot"></span><template v-if="event.type === 'retrieval.action_completed'">{{ event.data.strategy || '检索' }} · {{ event.data.source_type }} · 找到 {{ event.data.result_count }} 个结果 <span>{{ Math.round(event.data.latency_ms) }} ms</span></template><template v-else>构建上下文 · {{ event.data.item_count }} 条材料</template></li></ul>
                <div v-if="run.summary.stages?.length" class="stage-grid"><div v-for="(stage, index) in run.summary.stages" :key="index"><span>{{ stageName(stage.stage) }}</span><span>{{ (stage.latency_ms / 1000).toFixed(1) }} s</span></div></div>
                <div v-for="step in run.summary.steps" :key="step.step" class="tool-step">步骤 {{ step.step }} · {{ step.tools.join(' / ') }} · 新增 {{ step.new_evidence_count }} 条证据</div>
                <div v-if="run.summary.coverage?.length" class="coverage"><span v-for="item in run.summary.coverage" :key="item.requirement_id" :class="item.state">{{ item.requirement_id }} · {{ item.state }}</span></div>
                <p v-if="!run.activity?.length && !run.summary.stages?.length" class="subtle">执行事件将在实际动作完成后显示。</p>
              </details>
              <div v-if="run.evidence.length" class="evidence-links"><span>参考证据</span><button v-for="item in run.evidence" :key="item.label" @click="openEvidence(run, item.label)"><FileCode2 :size="13" /> {{ item.label }} <span>{{ item.citation.file_path.split(/[\\/]/).pop() }}</span></button></div>
              <div v-if="!run.worker_active" class="answer-actions"><button v-if="run.answer || run.drafts?.some(d => d.text)" class="text-button" @click="copyAnswer(run)">{{ copied === run.id ? '已复制' : '复制回答' }}</button><button v-if="['partial','failed','cancelled'].includes(run.status)" class="text-button" :disabled="chat.busy" @click="retry(run)">重新提问</button></div>
            </div>
          </article>
        </div>
      </div>
      <div class="composer-area">
        <p v-if="chat.error" class="form-error" role="alert">{{ chat.error }}</p>
        <p v-if="chat.connection" class="connection-notice" role="status">{{ chat.connection }}</p>
        <p v-if="active?.status === 'cancelled' && active.worker_active" class="connection-notice" role="status">正在等待当前调用结束，请稍候。</p>
        <div class="composer">
          <textarea ref="input" v-model="draft" aria-label="输入问题" placeholder="输入代码问题…" title="Enter 发送 · Shift + Enter 换行" rows="1" maxlength="10000" @keydown="keydown"></textarea>
          <div class="profile-control"><label class="sr-only" for="profile">回答模式</label><select id="profile" v-model="profile" title="Fast：快速理解；Full：深入解释" :disabled="chat.busy"><option value="fast">Fast</option><option value="full">Full</option></select></div>
          <button v-if="active && ['running','queued'].includes(active.status)" class="send-button stop" aria-label="停止生成" @click="chat.cancel(active)"><Square :size="16" /></button>
          <button v-else class="send-button" aria-label="发送问题" :disabled="chat.busy || !draft.trim()" @click="send"><ArrowUp :size="21" /></button>
        </div>
      </div>
    </main>

    <div v-if="selectedEvidence" class="drawer-scrim" @click="selectedEvidence = null"></div>
    <section v-if="selectedEvidence" class="evidence-drawer" role="dialog" aria-modal="true" aria-labelledby="evidence-title" @keydown.esc="selectedEvidence = null"><header><div><span class="eyebrow">SOURCE EVIDENCE</span><h2 id="evidence-title">证据 {{ selectedEvidence.label }}</h2></div><button class="icon-button" aria-label="关闭证据" autofocus @click="selectedEvidence = null"><X :size="21" /></button></header><template v-if="selectedEvidence.evidence"><div class="source-meta"><FileCode2 :size="18" /><strong>{{ selectedEvidence.evidence.citation.file_path }}</strong><span v-if="selectedEvidence.evidence.citation.start_line">第 {{ selectedEvidence.evidence.citation.start_line }}–{{ selectedEvidence.evidence.citation.end_line }} 行</span><span v-if="selectedEvidence.evidence.citation.class_name">{{ selectedEvidence.evidence.citation.class_name }}<template v-if="selectedEvidence.evidence.citation.symbol_name">#{{ selectedEvidence.evidence.citation.symbol_name }}</template></span><span>Chunk {{ selectedEvidence.evidence.chunk_id }} · {{ selectedEvidence.evidence.citation.source_type }}</span></div><p v-if="selectedEvidence.evidence.truncated" class="status-notice partial">此证据已按回答预算截断。</p><pre class="source-code">{{ selectedEvidence.evidence.content }}</pre></template><p v-else class="status-notice partial">此引用暂时无法对应到真实证据，不提供推测的文件或内容。</p></section>

    <div v-if="renameId || deleteId" class="modal-backdrop" @click.self="renameId = ''; deleteId = ''"><section class="modal" role="dialog" aria-modal="true" :aria-label="renameId ? '重命名会话' : '删除会话'"><h2>{{ renameId ? '重命名会话' : '删除这段会话？' }}</h2><input v-if="renameId" v-model="renameTitle" aria-label="会话标题" maxlength="200" @keydown.enter="confirmRename" /><p v-else>会话中的问题、回答与执行记录将一并删除。</p><div><button @click="renameId = ''; deleteId = ''">取消</button><button v-if="renameId" :disabled="!renameTitle.trim()" @click="confirmRename">保存</button><button v-else class="danger" @click="confirmDelete">删除会话</button></div></section></div>
  </div>
</template>
