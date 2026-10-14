<script setup lang="ts">
import { computed } from 'vue'
import type { Run } from './types'
import MarkdownBlock from './MarkdownBlock.vue'
const props = defineProps<{ run: Run }>()
const labels = computed(() => props.run.evidence.map(e => e.label))
const drafts = computed(() => (props.run.drafts || []).filter(d => d.state !== 'committed' && d.state !== 'superseded'))
</script>
<template>
  <MarkdownBlock v-for="section in run.sections" :key="section.section_id" :source="section.markdown" :labels="labels" :run-id="run.id" />
  <MarkdownBlock v-if="!run.sections.length && run.answer" :source="run.answer" :labels="labels" :run-id="run.id" />
  <div v-for="draft in drafts" :key="`${draft.generation_id}:${draft.section_id}`" class="draft-section">
    <MarkdownBlock :source="draft.text" :labels="labels" preview :run-id="run.id" />
    <p v-if="draft.text && draft.state === 'interrupted'" class="status-notice">这部分内容尚未完成，请结合证据核实。</p>
  </div>
</template>
