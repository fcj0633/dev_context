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
  return escaped.replace(/\[(E[1-9]\d*|C[1-9]\d*)\]/g, '<button class="citation" data-citation="$1" aria-label="查看证据 $1">$1</button>')
}
const fence = md.renderer.rules.fence!
md.renderer.rules.fence = (tokens, idx, options, env, self) => `<div class="code-block"><button class="copy-code" type="button">复制代码</button>${fence(tokens, idx, options, env, self)}</div>`
export const renderMarkdown = (source: string) => md.render(source)
