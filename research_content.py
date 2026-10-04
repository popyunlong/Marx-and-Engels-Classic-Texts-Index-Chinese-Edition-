"""Conservative, deterministic screening of catalogue content, with audit evidence.

Missing abstracts, authors, DOI and pagination are never exclusion criteria.
Rules use complete administrative labels, explicit columns/types or book citations,
not loose keywords that might occur in a scholarly paper's title.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

VERSION = 1

# Kept public for existing collector callers; anchors deliberately avoid titles
# such as 'Editorial boards and academic freedom' or 'Contents of belief'.
ADMIN_TITLE = re.compile(
    r"^(?:(?:front|back)\s*(?:cover|matter)|cover\s*image|table of contents|contents|"
    r"issue information|editorial board|masthead|notes on contributors|"
    r"list of (?:contributors|reviewers|referees)|instructions (?:to|for) authors|"
    r"subscription information|acknowledg(?:e)?ments? to (?:reviewers|referees)|advertisements?|call for papers|index to volume)"
    r"(?:\s*[:：\-–—].*|\s+(?:volume|vol\.?|issue|\d).*)?$|"
    r"^(?:[A-Z]+\s+)?(?:volume|vol\.?)\s+\d+\s+(?:issue|no\.?)\s+\d+\s+cover and (?:front|back) matter$|"
    r"^(?:封面|封底|目录|编委会名单|编委名单|作者简介|征稿启事|投稿须知|订阅须知|订阅信息|广告)(?:[：:].*)?$",
    re.I,
)


def assess(a: dict) -> dict:
    title = re.sub(r"\s+", " ", str(a.get("title") or "")).strip().rstrip(".")
    declared = str(a.get("type") or "article").strip().lower()
    column = str(a.get("section_name") or a.get("content_category") or "").strip()
    authors = a.get("authors") or []
    if isinstance(authors, str):
        authors = re.split(r"[、;；]", authors)
    authors = [str(x).strip().strip("- ") for x in authors]

    def result(decision, rule, reason, kind=None, evidence=None):
        return {"version": VERSION, "decision": decision, "rule": rule,
                "reason": reason, "type": kind or declared,
                "evidence": evidence if evidence is not None else title}

    if ADMIN_TITLE.fullmatch(title):
        return result("exclude", "administrative-title", "封面、目录或期刊管理信息，不是学术正文", "other")
    if declared in {"advertisement", "front-matter", "back-matter", "cover", "toc", "announcement", "call-for-papers"}:
        return result("exclude", "administrative-type", "来源明确标为广告、刊务或通知", "other", declared)
    if column in {"广告", "征稿启事", "刊务信息", "订阅信息", "作者简介", "封面", "封底", "目录", "活动预告", "新书推广"}:
        return result("exclude", "administrative-column", "来源栏目明确属于刊务或推广", "other", column)
    if re.search(r"[（(](?:党员来信|读者来信)[）)]$", title) or column in {"党员来信", "读者来信"}:
        return result("exclude", "reader-mail", "读者来信栏目，非学术通信", "letter")
    if re.match(r"^统计图表[：:]", title) and "国家统计局" in authors:
        return result("exclude", "statistical-table", "统计指标图表，非研究论文；原目录仍保留", "other")
    if re.match(r"^(?:recently reissued|new book launch|book launch|event announcement)[：:]", title, re.I):
        return result("exclude", "book-promotion", "新书推广或活动通知，非期刊学术正文", "other")
    source = urlsplit(str(a.get("url") or ""))
    if source.hostname == "monthlyreview.org" and source.path.startswith("/new-") and re.search(r"\bhead to\b", title, re.I):
        return result("exclude", "publisher-event", "出版社新书活动推广，非期刊学术正文", "other", source.path)

    aliases = {"journal-article": "article", "book-review": "review", "book review": "review",
               "review essay": "review", "correction": "erratum", "response": "commentary"}
    kind = aliases.get(declared, declared)
    categories = {"book reviews": "review", "book review": "review", "书评": "review",
                  "review essay": "review", "editorial": "editorial", "编者按": "editorial",
                  "commentary": "commentary", "discussion": "commentary", "讨论": "commentary", "评论": "commentary", "interview": "interview", "访谈": "interview"}
    if column.lower() in categories:
        return result("keep", "scholarly-column", "按原刊栏目标明学术内容类型", categories[column.lower()], column)
    if any(x in {"本刊编辑部", "The Editors", "Editors"} for x in authors):
        return result("keep", "editor-author", "编辑部署名，标为编者按／评论", "editorial", authors)
    if "本刊评论员" in authors:
        return result("keep", "commentator-author", "本刊评论员署名，标为评论", "commentary", authors)
    if re.fullmatch(r"From the Editor(?:s)?|Editorial|Preface|Foreword", title, re.I):
        return result("keep", "editorial-title", "编者按或前言，保留但不标为研究论文", "editorial")
    if re.match(r"^(?:Book review|Review essay)\b", title, re.I) or (
        re.search(r"\bISBN\s*:?\s*[\dXx-]{10,}", title) and re.search(r"\b\d+\s*pp\.?", title, re.I)):
        return result("keep", "book-review-citation", "书评题名或完整书目描述，标为书评", "review")
    if re.search(r"(?:\b(?:an? )?(?:interview|conversation) with\b|[（(]访谈[）)]$|^访谈[：:])", title, re.I):
        return result("keep", "interview-title", "题名明确为访谈／对话", "interview")
    if re.search(r"\b(?:erratum|corrigendum)\b", title, re.I):
        return result("keep", "correction-title", "更正条目，单独标明类型", "erratum")
    if re.match(r"^(?:Reply|Replies|Response) to\b", title, re.I):
        return result("keep", "scholarly-response", "学术回应，标为评论", "commentary")
    if column.lower() in {"news", "news and views", "reportage", "新闻", "人物报道", "通讯", "资料", "文摘", "党刊精选"} or re.search(r"[（(]党刊精选[）)]$", title):
        return result("review", "non-paper-column", "报道、转载或资料栏目，需核对是否属于学术内容", "other", column or title)
    if kind not in {"article", "review", "editorial", "interview", "commentary", "letter", "erratum", "peer-review"}:
        return result("review", "unknown-type", "来源类型未明确为学术内容，请人工核对", "other", declared)
    return result("keep", "academic-candidate", "未命中非学术内容规则；仍须按原流程校订", kind)


def apply(a: dict) -> dict:
    policy = assess(a)
    a["content_policy"] = policy
    a["type"] = policy["type"]
    return a
