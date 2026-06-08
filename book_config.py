from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from runtime_env import CONFIG_DIR


BOOKS_CONFIG_PATH = CONFIG_DIR / "books.yaml"


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
            )
        )
    return configs or list(DEFAULT_BOOK_CONFIGS)


def book_keys(path: Path = BOOKS_CONFIG_PATH) -> list[str]:
    return [book.key for book in load_book_configs(path)]


def book_config_map(path: Path = BOOKS_CONFIG_PATH) -> dict[str, BookConfig]:
    return {book.key: book for book in load_book_configs(path)}
