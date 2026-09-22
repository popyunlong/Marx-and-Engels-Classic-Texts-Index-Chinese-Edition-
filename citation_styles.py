from __future__ import annotations

"""全站引文格式注册表。

这里是格式键、分组、别名、学科代码、默认模板和核验来源的唯一权威来源。
检索、阅读器、AI、插注校注 Agent、导出和后台都应从本模块取值，不得再各自
维护一份期刊清单。
"""

from dataclasses import asdict, dataclass
from typing import Iterable, Mapping


@dataclass(frozen=True)
class CitationStyle:
    key: str
    label: str
    category: str
    family: str
    aliases: tuple[str, ...] = ()
    subject_codes: tuple[str, ...] = ()
    source_url: str = ""
    source_date: str = ""
    reviewed_at: str = "2026-09-22"
    evidence_type: str = "最新官方投稿规范或近期公开刊文"
    enabled: bool = True
    requires_approval: bool = False


NATIONAL = "国家标准"
SOCIAL_SCIENCE = "综合社科"
MARXISM = "马克思主义期刊"
POLITICAL_ECONOMY = "政治经济学期刊"
CATEGORY_ORDER = (NATIONAL, SOCIAL_SCIENCE, MARXISM, POLITICAL_ECONOMY)


def _s(
    key: str,
    label: str,
    category: str,
    family: str,
    *,
    aliases: tuple[str, ...] = (),
    codes: tuple[str, ...] = (),
    url: str,
    date: str = "",
    evidence: str = "最新官方投稿规范或近期公开刊文",
    approval: bool = False,
) -> CitationStyle:
    return CitationStyle(
        key=key,
        label=label,
        category=category,
        family=family,
        aliases=aliases,
        subject_codes=codes,
        source_url=url,
        source_date=date,
        evidence_type=evidence,
        requires_approval=approval,
    )


# 顺序即所有下拉框的稳定显示顺序。旧的四个键保持不变。
CITATION_STYLES: tuple[CitationStyle, ...] = (
    _s("gb2025", "GB/T 7714—2025", NATIONAL, "gb2025", url="https://openstd.samr.gov.cn/", approval=True,
       evidence="国家标准公开系统；模板须后台审批"),
    _s("gb2015", "GB/T 7714—2015", NATIONAL, "gb2015", url="https://openstd.samr.gov.cn/",
       evidence="国家标准公开系统"),
    _s("zgshkx", "《中国社会科学》", SOCIAL_SCIENCE, "full_footnote",
       url="http://sscp.cssn.cn/zgshkx/"),

    _s("mkszyj", "《马克思主义研究》", MARXISM, "full_footnote", codes=("A81", "D6"),
       url="http://myy.cssn.cn/"),
    _s("zgdsyj", "《中共党史研究》", MARXISM, "separated_footnote", codes=("A81", "D5"),
       url="https://www.dswxyjy.org.cn/"),
    _s("mkszyyxs", "《马克思主义与现实》", MARXISM, "compact_footnote", codes=("A81", "D6"),
       url="http://www.cctb.net/"),
    _s("gwlldx", "《国外理论动态》", MARXISM, "publisher_year_footnote", codes=("A81", "D0-0", "D6"),
       url="http://www.cctb.net/"),
    _s("ddsjyshzy", "《当代世界与社会主义》", MARXISM, "edition_compact_footnote",
       url="http://www.cctb.net/"),
    _s("zgtsshzyyj", "《中国特色社会主义研究》", MARXISM, "colon_volume_bibliography",
       aliases=("中特研究",), codes=("A81", "G641", "D64"), url="https://www.zgtsshzy.net/"),
    _s("sxllyjdk", "《思想理论教育导刊》", MARXISM, "comma_volume_bibliography",
       codes=("A81", "G641", "D64"), url="https://www.sizhengwang.cn/"),
    _s("mkszylilxkyj", "《马克思主义理论学科研究》", MARXISM, "comma_volume_bibliography",
       url="https://mkszy.cbpt.cnki.net/"),
    _s("kxszy", "《科学社会主义》", MARXISM, "publisher_year_footnote", codes=("A81", "D2", "D6"),
       url="https://kxsh.cbpt.cnki.net/EditorAN/PromptPageInfo.aspx?c=1&t=v", evidence="最新官方投稿须知"),
    _s("ddwx", "《党的文献》", MARXISM, "publisher_year_footnote", codes=("A81", "D2", "D6"),
       url="https://www.dswxyjy.org.cn/"),
    _s("hqwg", "《红旗文稿》", MARXISM, "publisher_year_footnote", codes=("A81", "D23", "D6"),
       url="http://www.qstheory.cn/hqwg/"),
    _s("ddsjshzywt", "《当代世界社会主义问题》", MARXISM, "publisher_year_footnote", codes=("A81", "D6"),
       url="https://www.krics.sdu.edu.cn/xsqk/tgxz.htm", date="2025", evidence="2025 年官方投稿须知"),
    _s("jxyyj", "《教学与研究》", MARXISM, "comma_segmented_footnote", codes=("A81", "D6"),
       url="http://jxyyj.ruc.edu.cn/"),
    _s("llsy", "《理论视野》", MARXISM, "responsible_bibliography", codes=("A81", "D6"),
       url="http://www.dangjian.cn/"),
    _s("qs", "《求是》", MARXISM, "parenthesized_volume_footnote", codes=("A81", "D6"),
       url="https://www.qstheory.cn/20260615/058bd355f7fc4e8db56bb102d31d8e16/c.html", date="2026-06-15",
       evidence="近期官方刊文注释实例"),
    _s("shzyyj", "《社会主义研究》", MARXISM, "responsible_bibliography", codes=("A81", "D6"),
       url="https://socialismstudies.ccnu.edu.cn/"),
    _s("sxlljy", "《思想理论教育》", MARXISM, "responsible_bibliography", codes=("A81", "G641", "D64"),
       url="https://sllj.sjtu.edu.cn/"),
    _s("sxzzjyyj", "《思想政治教育研究》", MARXISM, "responsible_bibliography", codes=("A81", "G641", "D64"),
       url="http://szyj.cbpt.cnki.net/"),
    _s("mzddxpllyj", "《毛泽东邓小平理论研究》", MARXISM, "responsible_bibliography", codes=("A84", "D61"),
       url="https://www.sass.org.cn/"),
    _s("mzyj", "《毛泽东研究》", MARXISM, "colon_volume_bibliography", codes=("A84", "D61"),
       url="https://www.dswxyjy.org.cn/"),

    _s("zzjjxpl", "《政治经济学评论》", POLITICAL_ECONOMY, "parenthesized_volume_footnote",
       url="https://crpe.ruc.edu.cn/CN/column/item14.shtml", evidence="官方注释体例"),
    _s("ddjjyj", "《当代经济研究》", POLITICAL_ECONOMY, "colon_volume_bibliography",
       url="https://www.ncpssd.cn/journal/details?gch=97946X&langType=1&nav=1", evidence="公开期刊资料与近期刊文"),
    _s("jjzh", "《经济纵横》", POLITICAL_ECONOMY, "colon_volume_bibliography",
       url="https://jjzh.cbpt.cnki.net/EditorB2N/PromptPageInfo.aspx?c=1&t=v", evidence="官方投稿须知"),
    _s("jjxj", "《经济学家》", POLITICAL_ECONOMY, "responsible_bibliography",
       url="https://eshukan.com/displayj.aspx?jid=4378", date="2025", evidence="2025 年投稿说明与近期刊文"),
    _s("jjllyjjgl", "《经济理论与经济管理》", POLITICAL_ECONOMY, "author_year",
       url="http://jjll.ruc.edu.cn/"),
)

CITATION_STYLE_BY_KEY: Mapping[str, CitationStyle] = {style.key: style for style in CITATION_STYLES}
CITATION_FORMAT_KEYS: tuple[str, ...] = tuple(style.key for style in CITATION_STYLES)
CITATION_FORMAT_LABELS: Mapping[str, str] = {style.key: style.label for style in CITATION_STYLES}
JOURNAL_STYLE_KEYS: tuple[str, ...] = tuple(
    style.key for style in CITATION_STYLES if style.category in {MARXISM, POLITICAL_ECONOMY}
)
CITATION_STYLE_ALIASES: Mapping[str, str] = {
    alias: style.key for style in CITATION_STYLES for alias in style.aliases
}


# 模板面向已归一的结构化字段。{volume_segment} 已包含“第/卷/册”，
# 因此单卷本、上下卷和非“卷”制分册不会被模板误标。
FAMILY_DEFAULT_TEMPLATES: Mapping[str, str] = {
    "gb2025": "",
    "gb2015": "{title}{volume_gb}[M].{place}:{publisher},{year}{edition_suffix}:{page_range}{page_note}.",
    "full_footnote": "《{title}》{volume_segment}{publication_full}，{page}。",
    "parenthesized_volume_footnote": "《{title}》{volume_parenthesized}{publication_full}，{page}。",
    "publisher_year_footnote": "《{title}》{volume_segment}{publication_publisher_year}，{page}。",
    "separated_footnote": "《{title}》{volume_segment}{publication_separated}，{page}。",
    "compact_footnote": "《{title}》{volume_segment}{page}。",
    "edition_compact_footnote": "《{edition_title}》{edition_compact}{volume_segment}{page}。",
    "comma_segmented_footnote": "《{title}》{volume_comma}{publication_separated}，{page}。",
    "colon_volume_bibliography": "{title}{volume_colon}[M]{publication_bibliography_cn}：{page_range}{page_note}.",
    "comma_volume_bibliography": "{title}{volume_comma}[M]{publication_bibliography_cn}:{page_range}{page_note}.",
    "responsible_bibliography": "{responsibility_cn}{title}{volume_colon}[M]{responsibility_tail_cn}{publication_bibliography_cn}：{page_range}{page_note}.",
    "author_year": "{author_year_lead}《{title}》{volume_segment}{publication_author_year}，{page}。",
}

DEFAULT_CITATION_TEMPLATES: dict[str, str] = {
    style.key: FAMILY_DEFAULT_TEMPLATES[style.family] for style in CITATION_STYLES
}


def add_publication_segments(parts: Mapping[str, str]) -> dict[str, str]:
    """根据已知出版字段生成带分隔符的安全片段。

    片段连同它们左侧的标点一起生成；字段缺失时整段留空，不会产生
    “，：”、“xxxx年”或平白多出的句点。
    """
    output = {str(key): str(value or "") for key, value in parts.items()}
    place = output.get("place", "").strip()
    publisher = output.get("publisher", "").strip()
    raw_year = output.get("year", "").strip()
    year = "" if raw_year.lower() in {"xxxx", "unknown", "none"} else raw_year
    edition = output.get("edition_suffix", "").strip()

    if place and publisher:
        publisher_place = f"{place}：{publisher}"
    else:
        publisher_place = publisher or place

    full_bits = [value for value in (publisher_place, (year + "年" + edition) if year else edition) if value]
    separated_bits = [value for value in (publisher, year + "年" if year else "") if value]
    bibliography_bits = [value for value in (publisher_place, (year + edition) if year else edition) if value]
    publisher_year = ""
    if publisher and year:
        publisher_year = publisher + year + "年版"
    elif publisher:
        publisher_year = publisher
    elif year:
        publisher_year = year + "年版"

    output["publication_full"] = ("，" + "，".join(full_bits)) if full_bits else ""
    output["publication_publisher_year"] = ("，" + publisher_year) if publisher_year else ""
    output["publication_separated"] = ("，" + "，".join(separated_bits)) if separated_bits else ""
    output["publication_bibliography_cn"] = ("." + "，".join(bibliography_bits)) if bibliography_bits else ""
    output["publication_author_year"] = ("，" + publisher_place) if publisher_place else ""

    responsibility = output.get("responsibility_author_year", "").rstrip("，")
    lead_bits = [value for value in (responsibility, year) if value]
    output["author_year_lead"] = ("，".join(lead_bits) + "：") if lead_bits else ""
    output["year_value"] = year
    output["year_cn"] = year + "年" if year else ""
    return output


def style_options(*, gb2025_approved: bool, include_auto: bool = False) -> list[dict]:
    """返回可直接交给 Jinja/JSON 的分组选项。

    学科代码与别名是注册表数据，不放进下拉框文案。
    """
    groups: list[dict] = []
    for category in CATEGORY_ORDER:
        rows = []
        for style in CITATION_STYLES:
            if not style.enabled or style.category != category:
                continue
            if style.requires_approval and not gb2025_approved:
                continue
            rows.append({"key": style.key, "label": style.label})
        if rows:
            groups.append({"label": category, "styles": rows})
    if include_auto:
        groups.insert(0, {"label": "自动识别", "styles": [{"key": "auto", "label": "自动识别"}]})
    return groups


def flat_style_options(*, gb2025_approved: bool, include_auto: bool = False) -> list[dict]:
    return [row for group in style_options(gb2025_approved=gb2025_approved, include_auto=include_auto)
            for row in group["styles"]]


def style_registry_payload(styles: Iterable[CitationStyle] = CITATION_STYLES) -> list[dict]:
    """管理端/审计导出使用的完整注册表数据。"""
    return [asdict(style) for style in styles]


def resolve_style_key(value: str) -> str | None:
    value = str(value or "").strip()
    if value in CITATION_STYLE_BY_KEY:
        return value
    return CITATION_STYLE_ALIASES.get(value)
