export const STATUS = {
  answered: "已回答",
  insufficient: "证据不足",
  direct: "直接回复",
  not_found: "未找到",
  blocked: "已拦截",
  error: "出错",
  stopped: "已停止",
  running: "未完成"
};

export const CHANNEL = { web: "Web", telegram: "Telegram", cli: "CLI", eval: "评测" };

export const WARNING = {
  step_budget_exhausted: "达到步数上限",
  calls_rejected: "有调用被拒绝",
  history_sources_unavailable: "历史来源查询失败",
  history_unavailable: "对话记录读取失败",
  corpus_unavailable: "新闻库概况查询失败",
  low_relevance: "匹配度偏低",
  read_targets_dropped: "读取额度截掉了目标",
  semantic_skipped: "跳过语义召回",
  rerank_fallback: "重排失败，保留融合顺序",
  usage_inconsistent: "token 总数不一致",
  evidence_truncated: "证据块超出预算",
  citations_removed: "引用校验删除了内容"
};

export const WARNING_DETAIL = {
  step_budget_exhausted: "取证模型在最后一步仍提出了调用，取证因达到步数上限而停止；这一步的结果没有再经取证模型判断，直接进入综合。",
  calls_rejected: "部分调用没有执行，原因见调用表；取证模型收到的是拒绝说明。",
  history_sources_unavailable: "查询历史回答所引文章时出错，这些文章没有登记为证据。",
  history_unavailable: "读取这个用户的对话记录时出错，查看关注项时没有排除已经讲过的报道。",
  corpus_unavailable: "查询新闻库概况时出错，取证模型的系统提示里这一段只说明暂时无法获取。",
  low_relevance: "最相关结果的匹配度低于阈值，返回给取证模型的文本附有可能不切题的提示。",
  read_targets_dropped: "请求的目标超出本次请求的读取上限，超出部分作为读取失败返回给取证模型。",
  semantic_skipped: "没有取得查询向量，这次检索跳过了语义召回。",
  rerank_fallback: "重排调用失败，候选保留融合后的顺序。",
  usage_inconsistent: "服务商返回的 token 总数既不等于输入加输出，也不等于输入、输出、推理三项之和。",
  evidence_truncated: "证据块超出字数预算：先不放入历史来源，仍超出时去掉部分已读全文证据的正文，见证据表的证据块一列。",
  citations_removed: "引用校验删掉了回答中不可引用的编号、原始地址或数字引用，见引用校验。"
};

export const REASON = {
  call_limit: "超出单步上限",
  unknown_tool: "未知工具",
  duplicate: "与之前的调用相同",
  invalid_args: "参数错误",
  read_quota: "读取额度用尽"
};

export const LEVEL = { listed: "列出", summary: "摘要", fulltext: "全文" };

export const SORT = { relevance: "按相关性", importance: "按重要性", time: "按时间" };

export const ROUTE = { direct: "直接回复", not_found: "未找到", synthesize: "进入综合" };

export const ROLE = { system: "系统", human: "用户", ai: "模型", tool: "工具结果" };

export function warningLabel(code) {
  return WARNING[code] || code;
}
