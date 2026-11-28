import { SORT, STATUS } from "./labels";

// The span on which each request-level warning arises.
const PLACES = {
  step_budget_exhausted: (node) => node.kind === "step",
  evidence_truncated: (node) => node.kind === "llm" && node.name === "final",
  citations_removed: (node) => node.kind === "llm" && node.name === "final"
};

// The API returns spans flat with their parent ids; the request itself is the root of the tree.
export function buildTrace(detail) {
  const { run, spans } = detail;
  const nodes = new Map(
    spans.map((span) => [span.span_id, { ...span, key: span.span_id, warnings: [...(span.warnings || [])], children: [] }])
  );
  const top = [];
  for (const node of nodes.values()) {
    const parent = node.parent_span_id ? nodes.get(node.parent_span_id) : null;
    (parent ? parent.children : top).push(node);
  }
  const startMs = Date.parse(run.started_at);
  const ends = spans.map((span) => (span.finished_at_ms ?? span.started_at_ms) - startMs);
  const totalMs = Math.max(run.latency_ms ?? 0, ...ends, 1);
  const evidence = run.output?.evidence || [];
  const root = {
    key: "request",
    kind: "request",
    status: run.status === "error" ? "error" : run.status === "running" ? "aborted" : "ok",
    started_at_ms: startMs,
    finished_at_ms: run.latency_ms === null ? null : startMs + run.latency_ms,
    warnings: [],
    children: top.map((node) => foldClosingCall(nodes, node))
  };
  nodes.set(root.key, root);
  placeRequestWarnings(run.warnings || [], spans, root);
  return {
    run,
    root,
    nodes,
    startMs,
    totalMs,
    articles: detail.articles || {},
    evidenceByArticle: new Map(evidence.map((row) => [row.article_id, row]))
  };
}

// A gather call that proposes no tool call ends the gathering. Its step holds only that call, so the call takes
// the step's place and does not count as a step.
function foldClosingCall(nodes, node) {
  const call = node.kind === "step" && node.output?.calls?.length === 0 ? node.children.find((child) => child.kind === "llm") : null;
  if (!call) return node;
  nodes.delete(node.key);
  return Object.assign(call, { parent_span_id: null, closing: node.attributes?.step === 1 ? "direct" : "end" });
}

// The run row lists the warnings of every span as well; those recorded on the request itself are shown on the last
// span they concern, and the request keeps only those without one.
function placeRequestWarnings(codes, spans, root) {
  const onSpans = new Set(spans.flatMap((span) => span.warnings || []));
  for (const code of codes) {
    if (onSpans.has(code)) continue;
    const target = PLACES[code] ? root.children.findLast(PLACES[code]) : null;
    (target || root).warnings.push(code);
  }
}

// Every warning with the node it is shown on, in tree order.
export function warningsOf(trace) {
  const out = [];
  const walk = (node) => {
    node.warnings.forEach((code) => out.push({ code, node }));
    node.children.forEach(walk);
  };
  walk(trace.root);
  return out;
}

export function parentOf(trace, node) {
  return (node.parent_span_id && trace.nodes.get(node.parent_span_id)) || trace.root;
}

export function nodePath(trace, node) {
  const parts = [];
  for (let item = node; item.kind !== "request"; item = parentOf(trace, item)) {
    parts.unshift(item.kind === "tool" ? `${nodeTitle(item)} ${nodeSubtitle(trace, item)}` : nodeTitle(item));
  }
  return parts.join(" › ");
}

export function durationMs(node) {
  return node.finished_at_ms === null || node.finished_at_ms === undefined ? null : node.finished_at_ms - node.started_at_ms;
}

export function nodeTitle(node) {
  switch (node.kind) {
    case "request":
      return "请求";
    case "context":
      return "准备上下文";
    case "step":
      return `第 ${node.attributes?.step ?? "?"} 步`;
    case "llm":
      if (node.closing) return node.closing === "direct" ? "直接回复" : "结束取证";
      return node.name === "final" ? "综合模型" : "取证模型";
    case "embedding":
      return "向量化";
    case "retrieval":
      return "召回";
    case "rerank":
      return "重排";
    default:
      return node.name || node.kind;
  }
}

export function nodeSubtitle(trace, node) {
  const output = node.output || {};
  switch (node.kind) {
    case "request":
      return STATUS[trace.run.status] || trace.run.status;
    case "step":
      return output.calls?.length ? `${output.calls.length} 个调用` : "没有调用工具";
    case "llm":
      return node.attributes?.model || "";
    case "tool":
      if (node.name === "read_articles") return [...(node.input?.ids || []), ...(node.input?.urls || [])].join("、");
      if (node.name === "check_follows") return (output.follows || []).map((check) => check.follow).join("、");
      if (node.name === "search_news") return node.input?.query;
      return node.input?.topic || `${SORT[node.input?.sort] || ""}浏览`;
    case "retrieval":
      return `${output.candidates?.length ?? 0} 个候选`;
    case "rerank":
      return output.ranked ? `${output.ranked.length} 条` : "";
    default:
      return "";
  }
}

export function articleTitle(trace, articleId) {
  const article = trace.articles[String(articleId)];
  return article ? article.title_zh || article.title : `文章 ${articleId}`;
}

export function location(where) {
  if (!where) return "-";
  return where === "history" ? "历史" : `第 ${where.step} 步 #${where.call}`;
}

// Recall order, rerank order and the story leads of one search_news call, side by side; browse_news has no recall.
export function candidateRows(trace, tool) {
  const retrieval = tool.children.find((child) => child.kind === "retrieval");
  const rerank = tool.children.find((child) => child.kind === "rerank");
  const reranked = new Map((rerank?.output?.ranked || []).map((item, index) => [item.article_id, { rank: index + 1, score: item.score }]));
  const leads = new Set((tool.output?.stories || []).map((story) => story.lead));
  return (retrieval?.output?.candidates || []).map((candidate, index) => ({
    ...candidate,
    rank: index + 1,
    rerank: reranked.get(candidate.article_id) || null,
    lead: leads.has(candidate.article_id),
    title: articleTitle(trace, candidate.article_id)
  }));
}

export function rerankScores(tool) {
  const rerank = tool.children.find((child) => child.kind === "rerank");
  return new Map((rerank?.output?.ranked || []).map((item) => [item.article_id, item.score]));
}
