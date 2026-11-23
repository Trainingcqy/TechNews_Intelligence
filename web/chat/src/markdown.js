import DOMPurify from "dompurify";
import { marked, Renderer } from "marked";

function splitTrailingUrlSuffix(rawUrl) {
  let url = String(rawUrl || "").trim();
  if (!url) return { url: "", suffix: "" };

  let suffix = "";
  const trailingPunct = /[.,，。;；:：!！?？、]/;

  const hasMoreRightBrackets = (value, rightRe, leftRe) => {
    const rightCount = (value.match(rightRe) || []).length;
    const leftCount = (value.match(leftRe) || []).length;
    return rightCount > leftCount;
  };

  while (url) {
    const ch = url[url.length - 1];

    if (trailingPunct.test(ch)) {
      suffix = ch + suffix;
      url = url.slice(0, -1);
      continue;
    }

    if (ch === ")" && hasMoreRightBrackets(url, /\)/g, /\(/g)) {
      suffix = ch + suffix;
      url = url.slice(0, -1);
      continue;
    }

    if (ch === "）" && hasMoreRightBrackets(url, /）/g, /（/g)) {
      suffix = ch + suffix;
      url = url.slice(0, -1);
      continue;
    }

    if (ch === "]" && hasMoreRightBrackets(url, /\]/g, /\[/g)) {
      suffix = ch + suffix;
      url = url.slice(0, -1);
      continue;
    }

    break;
  }

  return { url, suffix };
}

const renderer = new Renderer();
renderer.link = function ({ href, text }) {
  const parsed = splitTrailingUrlSuffix(href);
  const safeHref = parsed.url || String(href || "").trim();
  const suffix = parsed.suffix || "";
  const rawText = String(text || "");
  const safeText = !rawText || rawText === href ? safeHref : rawText;
  return `<a href="${safeHref}" target="_blank" rel="noopener noreferrer">${safeText}</a>${suffix}`;
};
marked.setOptions({ renderer });

// Normalize section headings so they reliably render as styled (blue) <h2/h3>.
// Only h1-h3 are styled (styles.css), and marked needs a space after '#'. The model
// drifts between forms, so coerce them into the styled range deterministically:
//   "##标题"  (missing space)   -> "## 标题"
//   "#### 标题" (h4-h6)          -> "### 标题"
//   standalone "**标题**[:：]"   -> "## 标题"   (never a list item or labelled line)
function normalizeAgentHeadings(text) {
  const lines = String(text || "").split("\n");
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    // ATX heading missing the required space after '#', e.g. "##演讲核心内容".
    // Title must start with a letter/CJK char so "#1" / "#2024" stay untouched.
    let m = line.match(/^(\s{0,3})(#{1,6})([^\s#].*)$/);
    if (m && /^[\p{L}]/u.test(m[3])) {
      const hashes = m[2].length > 3 ? "###" : m[2];
      lines[i] = `${m[1]}${hashes} ${m[3]}`;
      continue;
    }

    // Well-formed heading deeper than h3 (h4-h6 are not styled blue) -> clamp to h3.
    m = line.match(/^(\s{0,3})#{4,6}(\s+.*)$/);
    if (m) {
      lines[i] = `${m[1]}###${m[2]}`;
      continue;
    }

    // Standalone bold line used as a section header: whole line is just "**title**"
    // optionally with a trailing colon. List items (start with -/*) and labelled
    // lines with content after the colon never match.
    m = line.match(/^\s{0,3}\*\*([^*\n]+?)\*\*[：:]?\s*$/);
    if (m) {
      const title = m[1].trim();
      if (title && title.length <= 30 && !/[。.!?！？]$/.test(title)) {
        lines[i] = `## ${title}`;
      }
    }
  }
  return lines.join("\n");
}

function answerMarkdown(rawText, sources) {
  const body = normalizeAgentHeadings(rawText);
  if (!Array.isArray(sources) || !sources.length) return body;

  const urlByN = {};
  for (const src of sources) urlByN[String(src.n)] = src.url;
  return body.replace(/\[(\d{1,3})\](?!\()/g, (full, n) => (
    urlByN[n] ? `[\\[${n}\\]](${urlByN[n]})` : full
  ));
}

// The reply comes from a model that reads external articles and the citation URLs come
// from external feeds, so the generated HTML is sanitized before it reaches v-html.
export function renderAnswerHtml(text, sources = []) {
  return DOMPurify.sanitize(marked.parse(answerMarkdown(text, sources)), { ADD_ATTR: ["target"] });
}
