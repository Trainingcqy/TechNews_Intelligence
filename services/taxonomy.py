"""Closed topic and kind vocabularies shared by the collection pipeline and the agent."""

from __future__ import annotations

TAXONOMY_VERSION = 1

TOPICS: dict[str, str] = {
    "ai": "人工智能",
    "software": "软件与开发",
    "chips": "芯片与算力",
    "devices": "消费电子",
    "security": "安全与隐私",
    "mobility": "汽车与机器人",
    "space_energy": "航天与能源",
    "internet": "互联网与应用",
    "science": "科学与医疗",
    "business": "商业与资本",
    "policy": "政策与法律",
    "general": "综合",
}

TOPIC_SCOPES: dict[str, str] = {
    "ai": "模型与智能体、AI 产品与应用、AI 研究与评测、AI 公司的各类动态",
    "software": "编程语言、框架、数据库、开发工具、开源项目、工程实践、云服务与运维",
    "chips": "半导体、GPU 与加速器、服务器、数据中心与算力建设",
    "devices": "手机、电脑、可穿戴、智能家居等终端设备及其系统",
    "security": "漏洞、攻击、数据泄露、安全工具、监控与隐私",
    "mobility": "电动车、自动驾驶、机器人、无人机",
    "space_energy": "火箭与卫星、核能与聚变、电网与储能、气候技术",
    "internet": "社交、内容、电商、搜索、流媒体、游戏等消费互联网产品与平台",
    "science": "生物、医药、物理、数学、气候等非计算领域的研究与进展",
    "business": "融资、并购、上市、财报、裁员、公司战略与人事",
    "policy": "立法、监管、诉讼、出口管制、政府采购与政府行动",
    "general": "不属于以上任何一类的内容：人文、历史、社会新闻、生活",
}

KINDS: dict[str, str] = {
    "news": "事件报道",
    "release": "发布",
    "research": "研究",
    "tutorial": "教程与实践",
    "opinion": "观点",
    "project": "项目",
    "discussion": "讨论",
    "promo": "推广",
    "other": "其他",
}

KIND_SCOPES: dict[str, str] = {
    "news": "对已发生事件的报道或官方公告：融资、并购、诉讼、政策、事故、人事",
    "release": "产品、模型、版本、功能的发布公告，以及对发布的报道",
    "research": "论文、研究成果、基准评测、数据报告",
    "tutorial": "操作指南、技术解析、工程复盘、客户案例",
    "opinion": "评论、分析、随笔、访谈",
    "project": "开源仓库、个人作品、工具展示",
    "discussion": "提问帖和社区讨论串",
    "promo": "活动宣传、促销、招聘等推广内容",
    "other": "以上都不适用",
}

EVENT_KINDS = ("news", "release", "research")

LEGACY_CATEGORY_MAP: dict[str, tuple[str, ...]] = {
    "AI": ("ai",),
    "安全": ("security",),
    "开发": ("software",),
    "商业": ("business",),
    "硬件": (),
    "生态": (),
}


def validate_topics(topics: list[str]) -> list[str]:
    if not 1 <= len(topics) <= 3:
        raise ValueError(f"topics must contain 1 to 3 keys, got {len(topics)}")
    if len(set(topics)) != len(topics):
        raise ValueError(f"duplicate topics: {topics}")
    unknown = [topic for topic in topics if topic not in TOPICS]
    if unknown:
        raise ValueError(f"unknown topics: {unknown}")
    if "general" in topics and len(topics) > 1:
        raise ValueError("general cannot be combined with other topics")
    return topics


def validate_kind(kind: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"unknown kind: {kind!r}")
    return kind
