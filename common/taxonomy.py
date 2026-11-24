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
    "ai": "models and agents, AI products and applications, AI research and evaluation, any news about AI companies",
    "software": "programming languages, frameworks, databases, developer tools, open-source projects, engineering "
                "practice, cloud services and operations",
    "chips": "semiconductors, GPUs and accelerators, servers, data centers and compute buildout",
    "devices": "phones, computers, wearables, smart home and other end-user devices and their operating systems",
    "security": "vulnerabilities, attacks, data breaches, security tools, surveillance and privacy",
    "mobility": "electric vehicles, autonomous driving, robots, drones",
    "space_energy": "rockets and satellites, nuclear power and fusion, power grids and storage, climate technology",
    "internet": "social media, content, e-commerce, search, streaming, games and other consumer internet products "
                "and platforms",
    "science": "biology, medicine, physics, mathematics, climate and other research outside computing",
    "business": "funding, acquisitions, IPOs, earnings, layoffs, company strategy and executives",
    "policy": "legislation, regulation, lawsuits, export controls, government procurement and government action",
    "general": "anything that fits none of the above: humanities, history, society, lifestyle",
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
    "news": "reporting on an event that has happened, or an official announcement: funding, acquisitions, lawsuits, "
            "policy, incidents, executive changes",
    "release": "the announcement of a product, model, version or feature, and reporting on such a release",
    "research": "papers, research results, benchmarks, data reports",
    "tutorial": "how-to guides, technical explanations, engineering retrospectives, customer case studies",
    "opinion": "commentary, analysis, essays, interviews, product reviews",
    "project": "open-source repositories, personal projects, tool showcases",
    "discussion": "questions and community discussion threads",
    "promo": "deals, discounts and coupons; shopping guides and product roundups such as 'best X'; event and ticket "
             "promotions; webinars and product demos; sponsored content; hiring; a site's own announcements and "
             "subscription appeals",
    "other": "none of the above applies",
}

EVENT_KINDS = ("news", "release", "research")


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
