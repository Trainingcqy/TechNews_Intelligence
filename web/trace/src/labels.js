export const STATUS = {
  answered: "已回答",
  insufficient: "证据不足",
  direct: "直接回复",
  not_found: "未找到",
  blocked: "已拦截",
  error: "出错",
  running: "未完成"
};

export const CHANNEL = { web: "Web", telegram: "Telegram", cli: "CLI", eval: "评测" };

export const WARNING = {
  step_budget_exhausted: "步数用尽",
  calls_rejected: "有调用被拒绝",
  seen_dates_unavailable: "阅读记录查询失败",
  history_sources_unavailable: "历史来源查询失败",
  follows_unavailable: "关注项查询失败",
  corpus_unavailable: "新闻库概况查询失败",
  low_relevance: "匹配度偏低",
  read_targets_dropped: "读取额度截掉了目标",
  semantic_skipped: "跳过语义召回",
  rerank_fallback: "重排失败，保留融合顺序",
  usage_inconsistent: "token 总数不一致",
  evidence_truncated: "证据块去掉了正文",
  citations_removed: "引用校验删除了内容"
};

export const REASON = {
  call_limit: "超出单步上限",
  unknown_tool: "未知工具",
  duplicate: "与之前的调用相同",
  invalid_args: "参数错误",
  read_quota: "读取额度用尽"
};

export const LEVEL = { listed: "列出", summary: "摘要", fulltext: "全文" };

export const ROUTE = { direct: "直接回复", not_found: "未找到", synthesize: "进入综合" };

export const ROLE = { system: "系统", human: "用户", ai: "模型", tool: "工具结果" };

export function warningLabel(code) {
  return WARNING[code] || code;
}
