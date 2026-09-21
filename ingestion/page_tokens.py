"""Canonical folio tokens; standalone ingestion has no dependency on the web app."""
import re
import unicodedata
from typing import Any

_ROMAN = re.compile(r"M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})$")
_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


def parse_label(value: Any) -> tuple[str, str, int] | None:
    token = unicodedata.normalize("NFKC", str(value or "")).strip()
    if re.fullmatch(r"[0-9]+", token) and int(token) > 0:
        return str(int(token)), "arabic", int(token)
    if token.lower().startswith("pre-"):
        token = token[4:]
    token = token.upper()
    if not token or not _ROMAN.fullmatch(token):
        return None
    values = [_VALUES[c] for c in token]
    number = sum(-v if i + 1 < len(values) and v < values[i + 1] else v
                 for i, v in enumerate(values))
    return "pre-" + token.lower(), "roman", number


