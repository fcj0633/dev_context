<script setup lang="ts">
import { ref, watch, onBeforeUnmount, nextTick } from 'vue'
import { renderMarkdown } from './markdown'
const props = defineProps<{ source: string; labels: string[]; preview?: boolean; runId: string }>()
const node = ref<HTMLElement | null>(null)
const html = ref('')
let last = 0, timer: ReturnType<typeof setTimeout> | undefined
async function render() {
  timer = undefined
  last = performance.now()
  html.value = renderMarkdown(props.source, { labels: props.labels, preview: !!props.preview })
  await nextTick()
  requestAnimationFrame(() => {
    const body = [...(node.value?.querySelectorAll('p,pre,li,td') || [])]
      .some(n => (n.textContent || '').replace(/\[E\d+\]/g, '').trim())
    const name = `devcontext:first-body:${props.runId}`
    if (body && typeof performance.mark === 'function' && !performance.getEntriesByName(name).length) {
      performance.mark(name)
    }
  })
}
watch(() => [props.source, props.labels, props.preview], () => {
  if (!last || performance.now() - last >= 50) { if (timer) clearTimeout(timer); void render() }
  else if (!timer) timer = setTimeout(() => { void render() }, 50 - (performance.now() - last))
}, { immediate: true })
onBeforeUnmount(() => { if (timer) clearTimeout(timer) })
</script>
<template><div ref="node" class="markdown" :class="{ 'draft-body': preview }" v-html="html"></div></template>
