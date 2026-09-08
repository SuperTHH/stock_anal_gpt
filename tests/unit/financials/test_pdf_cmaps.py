from io import BytesIO

import pytest
from pdfminer.cmapdb import CMapDB
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

from hengce.financials.pdf_cmaps import restore_declared_cmaps


def make_pdf(ordering="GB1", registry="Adobe", existing=False):
    mapping = CMapDB.get_unicode_map("Adobe-GB1").cid2unichr
    reverse = {char: cid for cid, char in mapping.items()}
    encoded = b"".join(reverse[c].to_bytes(2, "big") for c in "合并资产负债表")
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type0"),
            NameObject("/BaseFont"): NameObject("/Fixture"),
            NameObject("/Encoding"): NameObject("/Identity-H"),
            NameObject("/DescendantFonts"): ArrayObject(
                [
                    DictionaryObject(
                        {
                            NameObject("/Type"): NameObject("/Font"),
                            NameObject("/Subtype"): NameObject("/CIDFontType0"),
                            NameObject("/BaseFont"): NameObject("/Fixture"),
                            NameObject("/CIDSystemInfo"): DictionaryObject(
                                {
                                    NameObject("/Registry"): TextStringObject(registry),
                                    NameObject("/Ordering"): TextStringObject(ordering),
                                    NameObject("/Supplement"): NumberObject(4),
                                }
                            ),
                        }
                    )
                ]
            ),
        }
    )
    if existing:
        stream = DecodedStreamObject()
        stream.set_data(
            b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap endcmap end end"
        )
        font[NameObject("/ToUnicode")] = stream
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
        }
    )
    content = DecodedStreamObject()
    content.set_data(b"BT /F1 12 Tf 50 700 Td <" + encoded.hex().encode() + b"> Tj ET")
    page[NameObject("/Contents")] = content
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_restores_declared_adobe_cids_without_changing_original_bytes():
    payload = make_pdf()
    reader = PdfReader(BytesIO(payload))
    assert "合并资产负债表" not in reader.pages[0].extract_text()
    assert restore_declared_cmaps(reader) == 1
    assert reader.pages[0].extract_text() == "合并资产负债表"
    assert len(reader.pages[0]["/Resources"]["/Font"]["/F1"]["/ToUnicode"].get_data()) < 2000
    assert restore_declared_cmaps(reader) == 0
    assert "/ToUnicode" not in PdfReader(BytesIO(payload)).pages[0]["/Resources"]["/Font"]["/F1"]


@pytest.mark.parametrize(
    "kwargs", [{"registry": "Custom"}, {"ordering": "Identity"}, {"existing": True}]
)
def test_does_not_guess_unknown_mapping_or_replace_existing_mapping(kwargs, monkeypatch):
    reader = PdfReader(BytesIO(make_pdf(**kwargs)))
    monkeypatch.setattr(
        "hengce.financials.pdf_cmaps._document_text_cids",
        lambda _reader: pytest.fail("Unneeded content-stream scan"),
    )
    assert restore_declared_cmaps(reader) == 0


def test_resolves_indirect_font_resources_and_descendant_array():
    writer = PdfWriter(clone_from=BytesIO(make_pdf()))
    resources = writer.pages[0]["/Resources"]
    font = resources["/Font"]["/F1"]
    font[NameObject("/DescendantFonts")] = writer._add_object(font["/DescendantFonts"])
    resources[NameObject("/Font")] = writer._add_object(resources["/Font"])
    buffer = BytesIO()
    writer.write(buffer)
    reader = PdfReader(BytesIO(buffer.getvalue()))
    assert restore_declared_cmaps(reader) == 1
    assert reader.pages[0].extract_text() == "合并资产负债表"


def test_collects_text_cids_from_nested_form_resources():
    writer = PdfWriter(clone_from=BytesIO(make_pdf()))
    page = writer.pages[0]
    form = DecodedStreamObject()
    form.set_data(page.get_contents().get_data())
    form[NameObject("/Type")] = NameObject("/XObject")
    form[NameObject("/Subtype")] = NameObject("/Form")
    form[NameObject("/Resources")] = page["/Resources"]
    form[NameObject("/BBox")] = ArrayObject([NumberObject(v) for v in (0, 0, 600, 800)])
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/XObject"): DictionaryObject({NameObject("/Fm"): writer._add_object(form)}),
    })
    content = DecodedStreamObject()
    content.set_data(b"/Fm Do")
    page[NameObject("/Contents")] = content
    buffer = BytesIO()
    writer.write(buffer)
    reader = PdfReader(BytesIO(buffer.getvalue()))
    assert restore_declared_cmaps(reader) == 1
    assert "合并资产负债表" in reader.pages[0].extract_text()
