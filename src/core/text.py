from __future__ import annotations

import re
import unicodedata

_SLUG_MAX = 32


def slugify(title: str) -> str:
    """Filesystem-safe slug from a book title (max 32 chars, ascii, dashed)."""
    text = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text[:_SLUG_MAX].rstrip("-") or "book"
