"""Published monitoring scope for the research bulletin's membership catalogue.

Domestic titles mirror the 26 enabled journal-monitor accounts verified on
2026-10-14. Update this registry when the monitoring scope changes; imported
articles and the legacy full-text database do not define subscription coverage.
"""
from journal_alerts import DEFAULT_JOURNAL_SOURCES, _JOURNAL_CATALOG_GROUPS
from research_journals import chinese_name


DOMESTIC_JOURNALS = (
    "中国社会科学", "马克思主义研究", "中共党史研究", "马克思主义与现实",
    "国外理论动态", "当代世界与社会主义", "中国特色社会主义研究",
    "思想理论教育导刊", "马克思主义理论学科研究", "科学社会主义",
    "党的文献", "红旗文稿", "当代世界社会主义问题", "教学与研究",
    "理论视野", "求是", "社会主义研究", "思想理论教育", "思想政治教育研究",
    "毛泽东邓小平理论研究", "毛泽东研究", "政治经济学评论", "当代经济研究",
    "经济纵横", "经济学家", "经济理论与经济管理",
)


def research_source_catalog() -> dict:
    """Build coverage from the monitoring registries without database or I/O reads."""
    zh = [{"name": name} for name in DOMESTIC_JOURNALS]
    en = [{"name": source["name"], "name_zh": chinese_name(source["name"])}
          for source in DEFAULT_JOURNAL_SOURCES]
    by_name = {source["name"]: source for source in en}
    grouped = set()
    groups = []
    for title, subtitle, names in _JOURNAL_CATALOG_GROUPS:
        items = [by_name[name] for name in names if name in by_name and name not in grouped]
        grouped.update(item["name"] for item in items)
        if items:
            groups.append({"title": title, "subtitle": subtitle, "sources": items, "count": len(items)})
    other = [source for source in en if source["name"] not in grouped]
    if other:
        groups.append({"title": "其他国外期刊", "subtitle": "OTHER JOURNALS",
                       "sources": other, "count": len(other)})
    return {"zh": zh, "en": en, "en_groups": groups,
            "zh_count": len(zh), "en_count": len(en), "total": len(zh) + len(en)}
