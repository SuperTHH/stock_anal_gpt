"""Restore missing Unicode maps only for explicitly declared standard Adobe CIDs.

The reader is repaired in memory; source PDFs and their content hashes are untouched.
Embedded ToUnicode maps always take precedence. Custom/unknown fonts are not guessed.
"""

from functools import lru_cache

from pdfminer.cmapdb import CMapDB
from pypdf import PdfReader
from pypdf.generic import (
    ArrayObject,
    ByteStringObject,
    ContentStream,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    TextStringObject,
)


@lru_cache(maxsize=8)
def _unicode_cmap(collection: str, vertical: bool, cids: tuple[int, ...] | None) -> bytes:
    mapping = CMapDB.get_unicode_map(collection, vertical=vertical).cid2unichr
    items = mapping.items() if cids is None else ((cid, mapping.get(cid, "")) for cid in cids)
    entries = [
        f"<{cid:04X}> <{char.encode('utf-16-be').hex().upper()}>"
        for cid, char in sorted(items)
        if 0 <= cid <= 65535 and char
    ]
    lines = [
        "/CIDInit /ProcSet findresource begin",
        "12 dict begin",
        "begincmap",
        "/CMapName /HengceStandardUnicode def",
        "/CMapType 2 def",
        "1 begincodespacerange",
        "<0000> <FFFF>",
        "endcodespacerange",
    ]
    for offset in range(0, len(entries), 100):
        chunk = entries[offset : offset + 100]
        lines.extend([f"{len(chunk)} beginbfchar", *chunk, "endbfchar"])
    lines.extend(["endcmap", "CMapName currentdict /CMap defineresource pop", "end", "end"])
    return "\n".join(lines).encode("ascii")


def restore_declared_cmaps(reader: PdfReader) -> int:
    visited: set[int] = set()
    repaired = 0
    cids = _document_text_cids(reader)

    def visit(resources: DictionaryObject) -> None:
        nonlocal repaired
        if id(resources) in visited:
            return
        visited.add(id(resources))
        fonts = resources.get("/Font", DictionaryObject()).get_object()
        for reference in fonts.values():
            font = reference.get_object()
            if id(font) in visited:
                continue
            visited.add(id(font))
            encoding = font.get("/Encoding")
            if (
                font.get("/Subtype") != "/Type0"
                or "/ToUnicode" in font
                or encoding not in ("/Identity-H", "/Identity-V")
            ):
                continue
            descendants = font.get("/DescendantFonts")
            if descendants is None:
                continue
            descendants = descendants.get_object()
            if len(descendants) != 1:
                continue
            info = descendants[0].get_object().get("/CIDSystemInfo", {})
            if hasattr(info, "get_object"):
                info = info.get_object()
            ordering = info.get("/Ordering")
            if info.get("/Registry") != "Adobe" or ordering not in (
                "GB1",
                "CNS1",
                "Japan1",
                "Korea1",
            ):
                continue
            try:
                payload = _unicode_cmap(f"Adobe-{ordering}", encoding == "/Identity-V", cids)
            except CMapDB.CMapNotFound:
                continue
            stream = DecodedStreamObject()
            stream.set_data(payload)
            font[NameObject("/ToUnicode")] = stream
            repaired += 1
        objects = resources.get("/XObject", DictionaryObject()).get_object()
        for reference in objects.values():
            obj = reference.get_object()
            nested = obj.get("/Resources")
            if nested is not None:
                visit(nested.get_object())

    for page in reader.pages:
        resources = page.get("/Resources")
        if resources is not None:
            visit(resources.get_object())
    return repaired


def _document_text_cids(reader: PdfReader) -> tuple[int, ...] | None:
    """Collect encoded character pairs, including text inside nested form streams.

    This is a superset across all fonts, not a guess from font widths (characters
    with default widths can still occur). On any unsupported stream, use full maps.
    """
    cids: set[int] = set()
    seen: set[int] = set()

    def strings(operand: object) -> None:
        if isinstance(operand, (ByteStringObject, TextStringObject)):
            raw = operand.original_bytes
            cids.update(int.from_bytes(raw[i : i + 2], "big") for i in range(0, len(raw) - 1, 2))
        elif isinstance(operand, (list, ArrayObject)):
            for item in operand:
                strings(item)

    def scan(stream: object, resources: DictionaryObject) -> None:
        if stream is not None:
            content = ContentStream(stream, reader)
            for operands, operator in content.operations:
                if operator in (b"Tj", b"TJ", b"'", b'"'):
                    strings(operands)
        for reference in resources.get("/XObject", DictionaryObject()).get_object().values():
            obj = reference.get_object()
            if id(obj) in seen or obj.get("/Subtype") != "/Form":
                continue
            seen.add(id(obj))
            nested = obj.get("/Resources", resources).get_object()
            scan(obj, nested)

    try:
        for page in reader.pages:
            scan(page.get_contents(), page.get("/Resources", DictionaryObject()).get_object())
    except Exception:
        return None
    return tuple(sorted(cids))
