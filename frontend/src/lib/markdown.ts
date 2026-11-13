// [3b] Safe Markdown for AI-written text (architecture § 14.5): markdown-it with HTML disabled, no
// images, no autolinks, links only for validated `nm:` refs (other links stay plain text).
import MarkdownIt from 'markdown-it';
import type { AssistantSource } from '../api/types';
import { resolveNmLink } from './assistant';

export type RenderOpts = {
  /** refs seen in tool results so far (streaming); null/undefined = trust the stored message */
  allowedRefs?: ReadonlySet<string> | null;
  sources?: AssistantSource[];
};

type Env = RenderOpts & { __nm?: true };

const md = new MarkdownIt('default', {
  html: false,
  linkify: false,
  typographer: false,
  breaks: false,
});
md.disable(['image', 'autolink', 'html_inline', 'html_block']);
// Only nm: links are parsed as links; everything else stays text.
md.validateLink = (url: string) => /^nm:[a-z][a-z-]*\/[^\s<>"'`\\]{1,400}$/i.test(url);
md.normalizeLink = (url: string) => url.trim();
md.normalizeLinkText = (s: string) => s;

const escapeHtml = md.utils.escapeHtml;

/** Addresses, MACs and OIDs keep LTR inside RTL text. */
const LTR_RE =
  /\b(?:(?:\d{1,3}\.){3}\d{1,3}(?:\/\d{1,2})?|(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}|(?:[0-9a-f]{4}\.){2}[0-9a-f]{4}|\d+(?:\.\d+){5,}|IP-\d+)\b/gi;
function textWithLtr(s: string): string {
  let out = '';
  let last = 0;
  for (const m of s.matchAll(LTR_RE)) {
    out += escapeHtml(s.slice(last, m.index)) + `<span dir="ltr">${escapeHtml(m[0])}</span>`;
    last = (m.index ?? 0) + m[0].length;
  }
  return out + escapeHtml(s.slice(last));
}

md.renderer.rules.text = (tokens, idx) => textWithLtr(tokens[idx].content);
md.renderer.rules.code_inline = (tokens, idx) =>
  `<code dir="ltr">${escapeHtml(tokens[idx].content)}</code>`;
md.renderer.rules.code_block = (tokens, idx) =>
  `<pre dir="ltr"><code>${escapeHtml(tokens[idx].content)}</code></pre>\n`;
md.renderer.rules.fence = (tokens, idx) =>
  `<pre dir="ltr"><code>${escapeHtml(tokens[idx].content)}</code></pre>\n`;

for (const name of [
  'paragraph_open',
  'list_item_open',
  'heading_open',
  'th_open',
  'td_open',
  'blockquote_open',
]) {
  const prev = md.renderer.rules[name];
  md.renderer.rules[name] = (tokens, idx, options, env, self) => {
    tokens[idx].attrSet('dir', 'auto');
    return prev ? prev(tokens, idx, options, env, self) : self.renderToken(tokens, idx, options);
  };
}

// Links: resolve nm: refs; unresolvable ones render as their text only.
md.core.ruler.push('nm_links', (state) => {
  const env = state.env as Env;
  for (const block of state.tokens) {
    if (block.type !== 'inline' || !block.children) continue;
    const kids = block.children;
    const stack: boolean[] = [];
    for (const tok of kids) {
      if (tok.type === 'link_open') {
        const href = tok.attrGet('href') ?? '';
        const tgt = resolveNmLink(href, { allowedRefs: env.allowedRefs, sources: env.sources });
        if (!tgt) {
          tok.type = 'text';
          tok.tag = '';
          tok.content = '';
          tok.attrs = null;
          stack.push(false);
          continue;
        }
        tok.attrs = [['href', tgt.href]];
        if (tgt.kind === 'internal') tok.attrSet('data-nm', '1');
        else {
          tok.attrSet('target', '_blank');
          tok.attrSet('rel', 'noopener noreferrer nofollow');
        }
        stack.push(true);
      } else if (tok.type === 'link_close') {
        const ok = stack.pop();
        if (!ok) {
          tok.type = 'text';
          tok.tag = '';
          tok.content = '';
        }
      }
    }
  }
  return true;
});

/** HTML of an AI answer; safe to put into innerHTML (no raw HTML, no images, no foreign links). */
export function renderAnswer(src: string, opts: RenderOpts = {}): string {
  return md.render(src ?? '', { ...opts, __nm: true } as Env);
}
