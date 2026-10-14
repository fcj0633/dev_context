import MarkdownIt from 'markdown-it'
import hljs from 'highlight.js/lib/core'
import java from 'highlight.js/lib/languages/java'
import sql from 'highlight.js/lib/languages/sql'
hljs.registerLanguage('java', java)
hljs.registerLanguage('sql', sql)
const md = new MarkdownIt({ html: false, linkify: false, breaks: true,
  highlight(code, language) { return hljs.getLanguage(language) ? hljs.highlight(code, { language }).value : '' },
})
const textRule = md.renderer.rules.text!
md.renderer.rules.text = (tokens, idx, options, env, self) => {
  const escaped = textRule(tokens, idx, options, env, self)
  if (env?.preview) return escaped
  return escaped.replace(/\[(E[1-9]\d*|C[1-9]\d*)\]/g, (whole, label) => {
    if (env?.labels && !env.labels.includes(label)) return whole
    return `<button class="citation" data-citation="${label}" aria-label="查看证据 ${label}">${label}</button>`
  })
}
const fence = md.renderer.rules.fence!
md.renderer.rules.fence = (tokens, idx, options, env, self) => `<div class="code-block"><button class="copy-code" type="button">复制代码</button>${fence(tokens, idx, options, env, self)}</div>`
export const renderMarkdown = (source: string, env?: { labels: string[]; preview: boolean }) => md.render(source, env)
