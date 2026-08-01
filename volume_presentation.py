from __future__ import annotations

import re
from dataclasses import dataclass


# 只有这两套选编的目录题名日期经过逐册整理，可以安全地继续由目录计算首末时间。
# 其它书一律不用目录中的“任意年份”推算卷期，以免人物生卒年、注释年份等污染展示。
TRUSTED_TOC_DATE_BOOKS = frozenset({
    "建党以来重要文献选编",
    "建国以来重要文献选编",
})


@dataclass(frozen=True)
class VolumePresentation:
    heading: str
    subtitle: str


_WENJI_SUBTITLES = {
    1: "1843—1848年著作",
    2: "1848—1859年著作",
    3: "1864—1883年著作",
    4: "恩格斯1884—1895年著作",
    5: "《资本论》第一卷",
    6: "《资本论》第二卷",
    7: "《资本论》第三卷",
    8: "《资本论》手稿选编",
    9: "《反杜林论》《自然辩证法》",
    10: "书信选编",
}

_CURATED_SUBTITLES = {
    "毛泽东选集": {
        1: "第一次国内革命战争时期、第二次国内革命战争时期",
        2: "抗日战争时期（上）",
        3: "抗日战争时期（下）",
        4: "第三次国内革命战争时期",
    },
    "毛泽东文集": {
        1: "1921.1—1937.6",
        2: "1937.8—1942.12",
        3: "1943.1—1945.8",
        4: "1945.8—1947.12",
        5: "1948.1—1949.9",
        6: "1949.10—1955.12",
        7: "1956.1—1958.12",
        8: "1959.2—1975.7",
    },
    "邓小平文选": {
        1: "1938.1—1965.12",
        2: "1975—1982",
        3: "1982.9—1992.2",
    },
    "江泽民文选": {
        1: "1980.8—1997.8",
        2: "1997.9—2000.2",
        3: "2000.2—2004.9",
    },
    "胡锦涛文选": {
        1: "1988.6—2002.9",
        2: "2002.12—2007.10",
        3: "2007.12—2012.11",
    },
    "治国理政": {
        1: "2012.11—2014.6",
        2: "2014.8—2017.9",
        3: "2017.10—2020.1",
        4: "2020.2—2022.5",
        5: "2022.5—2024.12",
    },
    "习近平经济文选": {
        1: "2012.11—2024.12",
    },
}

_PART_LABELS = {1: "上册", 2: "中册", 3: "下册"}
_PART_BOOKS = {
    "十八大以来重要文献选编",
    "十九大以来重要文献选编",
    "二十大以来重要文献选编",
}

_CN_DIGITS = "零一二三四五六七八九"


def _cn_number(value: int) -> str:
    if value < 10:
        return _CN_DIGITS[value]
    if value < 20:
        return "十" + (_CN_DIGITS[value % 10] if value % 10 else "")
    if value < 100:
        return _CN_DIGITS[value // 10] + "十" + (_CN_DIGITS[value % 10] if value % 10 else "")
    return str(value)


def _default_heading(volume: int, unit: str, single_volume: bool) -> str:
    return "" if single_volume else f"第 {volume} {unit or '卷'}"


def _first_edition_subtitle(volume: int, display_title: str) -> str:
    if 1 <= volume <= 22:
        return "著作卷"
    if volume == 23:
        return "马克思《资本论》第一卷"
    if volume == 24:
        return "马克思《资本论》第二卷"
    if volume == 25:
        return "马克思《资本论》第三卷"
    if volume == 26:
        match = re.search(r"第\s*([一二三123])\s*册", display_title)
        part = match.group(1) if match else ""
        part = {"1": "一", "2": "二", "3": "三"}.get(part, part)
        return f"《剩余价值理论》{f'第{part}册' if part else ''}"
    if 27 <= volume <= 39:
        return "书信卷"
    if 40 <= volume <= 50:
        return "补卷"
    return ""


def _second_edition_subtitle(volume: int) -> str:
    if 1 <= volume <= 29:
        return "著作卷"
    if 30 <= volume <= 43:
        return "《资本论》及其手稿卷"
    if volume == 44:
        return "马克思《资本论》第一卷"
    if volume == 45:
        return "马克思《资本论》第二卷"
    if volume == 46:
        return "马克思《资本论》第三卷"
    if 47 <= volume <= 60:
        return "书信卷"
    if 61 <= volume <= 70:
        return "笔记卷"
    return ""


def _lenin_subtitle(display_title: str, volume: int) -> str:
    # 文件题名来自《列宁全集》第二版增订版的逐卷说明，比从篇章目录重新猜测年代可靠。
    prefix = re.compile(r"^列宁全集\s*2版增订\s*[（(]第\s*0*%d\s*卷[）)]\s*[：:]?\s*" % volume)
    value = prefix.sub("", str(display_title or "").strip()).strip()
    value = re.sub(r"\s*-\s*", "—", value)
    value = re.sub(r"(?<=\d)年—(?=\d{1,2}月)", "年", value)
    return value


def volume_presentation(
    book: str,
    volume: int,
    display_title: str,
    *,
    unit: str = "卷",
    single_volume: bool = False,
    trusted_date_span: str = "",
) -> VolumePresentation:
    """Return stable reader labels without changing corpus identities or URLs."""
    heading = _default_heading(volume, unit, single_volume)

    if book in TRUSTED_TOC_DATE_BOOKS:
        return VolumePresentation(heading, trusted_date_span)
    if book == "文集":
        return VolumePresentation(heading, _WENJI_SUBTITLES.get(volume, ""))
    if book == "全集":
        return VolumePresentation(heading, _first_edition_subtitle(volume, display_title))
    if book == "全集二版":
        return VolumePresentation(heading, _second_edition_subtitle(volume))
    if book == "列宁全集":
        return VolumePresentation(heading, _lenin_subtitle(display_title, volume))
    if book in _CURATED_SUBTITLES:
        return VolumePresentation(heading, _CURATED_SUBTITLES[book].get(volume, ""))
    if book in _PART_BOOKS:
        return VolumePresentation(_PART_LABELS.get(volume, heading), "")
    if book == "历次党代会报告":
        return VolumePresentation(f"{_cn_number(volume)}大", "全国代表大会报告")
    if book == "历届全会公报":
        term, session = divmod(volume, 100)
        if term and session:
            return VolumePresentation(f"{_cn_number(term)}届{_cn_number(session)}中全会", "公报")
    if book == "五年规划":
        return VolumePresentation(f"{_cn_number(volume)}五", str(display_title or "").strip())

    # 《选集》等只有卷次、没有独立卷题的书，不重复展示“某某选集第N卷”；单行本也不
    # 把文件名当副标题。卷号/书名已经分别位于左栏和抽屉标题中。
    return VolumePresentation(heading, "")
