from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from runtime_env import CONFIG_DIR


BOOKS_CONFIG_PATH = CONFIG_DIR / "books.yaml"
WESTERN_REVIEWED_CONFIG_PATH = CONFIG_DIR / "western_marxism_reviewed.yaml"


@dataclass(frozen=True)
class BookConfig:
    key: str
    title: str
    short_title: str
    citation_title: str
    folder: str
    sort_order: int
    publisher: str
    place: str
    tag_class: str
    # 是否对终端用户开放。False 时该书库仍可被索引/调试，但不在「篇章直达」等
    # 面向用户的入口中露出。新书库默认开放，方便逐步上线后再打开。
    available: bool = True
    # 单卷本（无卷次划分的独立著作，如各《学习纲要》《概论》）：引文不冠「第N卷」。
    # 默认 False（多卷本或分卷著作如《文集》《经济文选》第一卷仍照常出「第N卷」）。
    single_volume: bool = False
    # 多卷本中「个别卷不冠卷次」的卷号集合（如《治国理政》卷1 我们服务的是 2014 无卷次初版，
    # 引文应作《习近平谈治国理政》而非「第一卷」——「第一卷」是 2018 第2版才回溯标注的）。
    unnumbered_volumes: tuple[int, ...] = ()
    # 单行本的主要责任者，用于生成完整的国标/脚注引文。旧书库留空时
    # 仍保持「题名起首」的既有格式，不会改动已有引文。
    authors: tuple[str, ...] = ()
    translators: tuple[str, ...] = ()
    # 阅读器与 AI 检索使用的专题键；空表示仍作为普通独立书库。
    collection: str = ""
    # 分卷单位。绝大多数著作以「卷」分卷，故默认「卷」；但《建党以来重要文献选编》
    # 《建国以来重要文献选编》原书封面标的是「第十七册」，引文须作「第17册」才与原书相符。
    # 注意：后台自定义引文模板里写死了「第{volume}卷」，故非「卷」的书库会绕过模板走
    # 程序化权威串（同 single_volume 的处理），避免模板把「册」错标成「卷」。
    volume_unit: str = "卷"
    # 个别多卷本使用「上卷 / 下卷」「第三卷（上）/ 第三卷（下）」等非数字卷标。
    # 这里保存 (volume, label) 对；引文层直接使用 label，避免机械生成错误的「第4卷」。
    volume_labels: tuple[tuple[int, str], ...] = ()


DEFAULT_BOOK_CONFIGS: tuple[BookConfig, ...] = (
    BookConfig(
        key="文集",
        title="《马克思恩格斯文集》",
        short_title="《文集》",
        citation_title="马克思恩格斯文集",
        folder="pdfs/文集",
        sort_order=10,
        publisher="人民出版社",
        place="北京",
        tag_class="wenji",
    ),
    BookConfig(
        key="全集",
        title="《马克思恩格斯全集》",
        short_title="《全集》",
        citation_title="马克思恩格斯全集",
        folder="pdfs/全集",
        sort_order=20,
        publisher="人民出版社",
        place="北京",
        tag_class="quanji",
    ),
)


def _clean_path(value: str) -> str:
    return str(Path(value).as_posix()) if value else ""


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("false", "0", "no", "off", "否", "停用", "关闭"):
        return False
    if text in ("true", "1", "yes", "on", "是", "启用", "开放"):
        return True
    return default


def load_book_configs(path: Path = BOOKS_CONFIG_PATH) -> list[BookConfig]:
    if not path.exists():
        return list(DEFAULT_BOOK_CONFIGS)

    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw_books = payload.get("books") if isinstance(payload, dict) else None
    if not isinstance(raw_books, list):
        return list(DEFAULT_BOOK_CONFIGS)

    configs: list[BookConfig] = []
    for index, item in enumerate(raw_books, start=1):
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        if not key:
            continue
        title = str(item.get("title") or f"《{key}》").strip()
        short_title = str(item.get("short_title") or title).strip()
        citation_title = str(item.get("citation_title") or title.strip("《》")).strip()
        folder = _clean_path(str(item.get("folder") or f"pdfs/{key}").strip())
        configs.append(
            BookConfig(
                key=key,
                title=title,
                short_title=short_title,
                citation_title=citation_title,
                folder=folder,
                sort_order=_coerce_int(item.get("sort_order"), index * 10),
                publisher=str(item.get("publisher") or payload.get("publisher") or "人民出版社").strip(),
                place=str(item.get("place") or payload.get("place") or "北京").strip(),
                tag_class=str(item.get("tag_class") or f"book-{index}").strip(),
                available=_coerce_bool(item.get("available"), True),
                single_volume=_coerce_bool(item.get("single_volume"), False),
                unnumbered_volumes=tuple(
                    int(v) for v in (item.get("unnumbered_volumes") or [])
                    if str(v).strip().lstrip("-").isdigit()
                ),
                authors=tuple(
                    str(v).strip() for v in (item.get("authors") or []) if str(v).strip()
                ),
                translators=tuple(
                    str(v).strip() for v in (item.get("translators") or []) if str(v).strip()
                ),
                collection=str(item.get("collection") or "").strip(),
                volume_unit=str(item.get("volume_unit") or "卷").strip() or "卷",
                volume_labels=tuple(
                    (int(k), str(v).strip())
                    for k, v in (item.get("volume_labels") or {}).items()
                    if str(k).strip().lstrip("-").isdigit() and str(v).strip()
                ),
            )
        )
    # 西马增量的逐卷证据与书目元数据集中保存在独立复核表中。运行时按 key 合并为
    # 41 个书目（《日常生活批判》三卷只生成一个 BookConfig），避免三卷被误当三本书。
    reviewed_path = path.parent / WESTERN_REVIEWED_CONFIG_PATH.name
    if reviewed_path.exists():
        reviewed_payload = yaml.safe_load(reviewed_path.read_text(encoding="utf-8")) or {}
        existing = {cfg.key for cfg in configs}
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in reviewed_payload.get("records") or []:
            if isinstance(row, dict) and str(row.get("key") or "").strip():
                grouped.setdefault(str(row["key"]).strip(), []).append(row)
        # 210—250 is reserved for this increment; 167—192 are already used by
        # Hegel/Kant/Feuerbach and must not be silently reordered.
        for offset, (key, rows) in enumerate(grouped.items(), start=210):
            if key in existing:
                continue
            first = rows[0]
            citation_title = str(first.get("citation_title") or key).strip()
            configs.append(BookConfig(
                key=key,
                title=f"《{citation_title}》",
                short_title=f"《{citation_title}》",
                citation_title=citation_title,
                folder="pdfs/西马文库",
                sort_order=offset,
                publisher=str(first.get("publisher") or "").strip(),
                place=str(first.get("place") or "").strip(),
                tag_class="western-marxism expanded",
                available=True,
                single_volume=len(rows) == 1,
                authors=tuple(str(v).strip() for v in first.get("authors") or [] if str(v).strip()),
                translators=tuple(str(v).strip() for v in first.get("translators") or [] if str(v).strip()),
                collection="western_marxism",
            ))
    return configs or list(DEFAULT_BOOK_CONFIGS)


def book_keys(path: Path = BOOKS_CONFIG_PATH) -> list[str]:
    return [book.key for book in load_book_configs(path)]


def book_config_map(path: Path = BOOKS_CONFIG_PATH) -> dict[str, BookConfig]:
    return {book.key: book for book in load_book_configs(path)}
