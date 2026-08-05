from __future__ import annotations

from urllib.parse import urlparse

OFFICIAL_PDF_SOURCE_IDS = frozenset({"cninfo", "sse", "szse"})


def is_official_pdf_location(source_id: str, source_url: object) -> bool:
    if source_id not in OFFICIAL_PDF_SOURCE_IDS or source_url is None:
        return False
    return urlparse(str(source_url)).path.casefold().endswith(".pdf")


__all__ = ["OFFICIAL_PDF_SOURCE_IDS", "is_official_pdf_location"]
