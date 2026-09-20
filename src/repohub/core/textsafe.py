"""Sanitising of untrusted text (API data) before it reaches a terminal or template."""
from __future__ import annotations

import re

_BIDI = "‪‫‬‭‮⁦⁧⁨⁩‎‏"
_ALWAYS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f" + _BIDI + "]")
_KEEP_NL = re.compile("[\x00-\x08\x0b\x0c\r\x0e-\x1f\x7f-\x9f" + _BIDI + "]")
_WS = re.compile(r"\s+")


def clean_text(value: str | None, multiline: bool = False) -> str:
    if value is None:
        return ""
    try:
        text = str(value)
        if multiline:
            return _KEEP_NL.sub("", text)
        text = _ALWAYS.sub("", text.replace("\r", " ").replace("\n", " ").replace("\t", " "))
        return _WS.sub(" ", text).strip()
    except Exception:
        return ""
