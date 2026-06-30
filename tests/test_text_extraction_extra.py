"""Extra coverage for app.utils.text_extraction — pdfplumber, pdfminer, docx, odt, zotero edge paths."""
import sys
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.utils import text_extraction
from app.utils.text_extraction import (
    _bbox_overlap,
    _extract_docx,
    _extract_html,
    _extract_odt,
    _extract_pdf,
    _extract_pdf_pdfminer,
    _extract_pdf_pdfplumber,
    _extract_zotero_snapshot,
    _split_on_sentences,
    chunk_text,
)


# ---------------------------------------------------------------------------
# _extract_html — fallback path when bs4 is missing
# ---------------------------------------------------------------------------


def test_extract_html_without_bs4(monkeypatch, tmp_path):
    """When bs4 is unavailable, _extract_html falls back to regex tag-strip."""
    f = tmp_path / "p.html"
    f.write_text("<p>Hello &amp; bye</p><script>x</script>", encoding="utf-8")
    monkeypatch.setitem(sys.modules, "bs4", None)
    out = _extract_html(f)
    # Regex strips tags but keeps content (script body included in regex path).
    assert "Hello & bye" in out


# ---------------------------------------------------------------------------
# _extract_zotero_snapshot — empty zip, bad zip, bs4-missing
# ---------------------------------------------------------------------------


def test_extract_zotero_snapshot_no_html(tmp_path):
    """A ZIP with no HTML returns empty string."""
    z = tmp_path / "empty.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("readme.txt", "no html here")
    assert _extract_zotero_snapshot(z) == ""


def test_extract_zotero_snapshot_bad_zip(tmp_path):
    """A corrupt ZIP returns empty string instead of raising."""
    z = tmp_path / "bad.zip"
    z.write_bytes(b"not really a zip file")
    assert _extract_zotero_snapshot(z) == ""


def test_extract_zotero_snapshot_prefers_index(tmp_path):
    """When several HTML files are present, index.html wins."""
    z = tmp_path / "snap.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("other.html", "<p>other</p>")
        zf.writestr("index.html", "<p>main</p>")
    out = _extract_zotero_snapshot(z)
    assert "main" in out and "other" not in out


def test_extract_zotero_snapshot_without_bs4(monkeypatch, tmp_path):
    """Without bs4 we still extract via regex tag-strip."""
    z = tmp_path / "snap.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("index.html", "<p>plain &amp; clear</p><style>x</style>")
    monkeypatch.setitem(sys.modules, "bs4", None)
    out = _extract_zotero_snapshot(z)
    assert "plain & clear" in out


# ---------------------------------------------------------------------------
# _extract_pdf — pdfplumber happy + fallback to pdfminer
# ---------------------------------------------------------------------------


def test_extract_pdf_pdfplumber_missing_returns_empty(monkeypatch, tmp_path):
    """If pdfplumber import fails, the helper returns empty string."""
    monkeypatch.setitem(sys.modules, "pdfplumber", None)
    p = tmp_path / "x.pdf"
    p.write_bytes(b"%PDF-1.4")
    assert _extract_pdf_pdfplumber(p) == ""


def test_extract_pdf_pdfplumber_exception_logged(monkeypatch, tmp_path):
    """A pdfplumber exception is caught and logged — returns empty string."""
    fake_pdfplumber = MagicMock()
    fake_pdfplumber.open.side_effect = RuntimeError("corrupt pdf")
    monkeypatch.setitem(sys.modules, "pdfplumber", fake_pdfplumber)
    p = tmp_path / "broken.pdf"
    p.write_bytes(b"%PDF-?")
    assert _extract_pdf_pdfplumber(p) == ""


def test_extract_pdf_pdfplumber_extracts_table_and_text(monkeypatch, tmp_path):
    """pdfplumber path joins table cells with tabs and skips bbox-overlapping text."""
    # Build fake table → rows ["A", "B"] / ["1", "2"]
    fake_table = MagicMock()
    fake_table.bbox = (0, 0, 100, 50)
    fake_table.extract.return_value = [["A", "B"], ["1", "2"]]

    fake_remaining = MagicMock()
    fake_remaining.filter.return_value = fake_remaining
    fake_remaining.extract_text.return_value = "outside text"

    fake_page = MagicMock()
    fake_page.find_tables.return_value = [fake_table]
    fake_page.filter.return_value = fake_remaining
    # When `remaining` chain is built from `page` itself, .filter is on page.

    fake_pdf = MagicMock()
    fake_pdf.pages = [fake_page]
    fake_pdf.__enter__ = lambda self: fake_pdf
    fake_pdf.__exit__ = lambda *_a: None

    fake_pdfplumber = MagicMock()
    fake_pdfplumber.open.return_value = fake_pdf
    monkeypatch.setitem(sys.modules, "pdfplumber", fake_pdfplumber)

    p = tmp_path / "x.pdf"
    p.write_bytes(b"%PDF")
    out = _extract_pdf_pdfplumber(p)
    assert "A\tB" in out
    assert "1\t2" in out


def test_extract_pdf_pdfplumber_no_tables(monkeypatch, tmp_path):
    """Pages without tables fall through to plain extract_text."""
    fake_page = MagicMock()
    fake_page.find_tables.return_value = []
    fake_page.extract_text.return_value = "just plain text"

    fake_pdf = MagicMock()
    fake_pdf.pages = [fake_page]
    fake_pdf.__enter__ = lambda self: fake_pdf
    fake_pdf.__exit__ = lambda *_a: None

    fake_pdfplumber = MagicMock()
    fake_pdfplumber.open.return_value = fake_pdf
    monkeypatch.setitem(sys.modules, "pdfplumber", fake_pdfplumber)

    p = tmp_path / "x.pdf"
    p.write_bytes(b"%PDF")
    out = _extract_pdf_pdfplumber(p)
    assert "just plain text" in out


def test_extract_pdf_falls_back_to_pdfminer(monkeypatch, tmp_path):
    """If pdfplumber returns empty, _extract_pdf falls back to pdfminer."""
    monkeypatch.setattr(text_extraction, "_extract_pdf_pdfplumber", lambda _: "")
    monkeypatch.setattr(text_extraction, "_extract_pdf_pdfminer", lambda _: "miner output")
    p = tmp_path / "x.pdf"
    p.write_bytes(b"%PDF")
    assert _extract_pdf(p) == "miner output"


def test_extract_pdf_pdfminer_handles_exception(monkeypatch, tmp_path):
    """A pdfminer exception is logged and returns empty string."""
    fake_module = MagicMock()
    fake_module.high_level.extract_text.side_effect = RuntimeError("boom")
    monkeypatch.setitem(sys.modules, "pdfminer", fake_module)
    monkeypatch.setitem(sys.modules, "pdfminer.high_level", fake_module.high_level)
    p = tmp_path / "x.pdf"
    p.write_bytes(b"%PDF")
    assert _extract_pdf_pdfminer(p) == ""


# ---------------------------------------------------------------------------
# _extract_docx
# ---------------------------------------------------------------------------


def test_extract_docx_handles_exception(monkeypatch, tmp_path):
    """If python-docx raises, the helper returns empty string."""
    fake_docx_module = MagicMock()
    fake_docx_module.Document.side_effect = RuntimeError("not a docx")
    monkeypatch.setitem(sys.modules, "docx", fake_docx_module)
    p = tmp_path / "x.docx"
    p.write_bytes(b"PK")
    assert _extract_docx(p) == ""


def test_extract_docx_paragraphs_and_table(monkeypatch, tmp_path):
    """Paragraphs and table rows are stitched together."""
    NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

    def make_node(tag, text=None, children=None):
        n = MagicMock()
        n.tag = NS + tag
        n.text = text
        n.iter = MagicMock(return_value=iter(children or [n]))
        n.findall = MagicMock(return_value=[])
        n.get = MagicMock(return_value=None)
        return n

    para = make_node("p", text="Hello world")
    para.iter = lambda: iter([para])

    cell = make_node("tc", text="A1")
    cell.iter = lambda: iter([cell])
    row = make_node("tr")
    row.findall = MagicMock(return_value=[cell])
    tbl = make_node("tbl")
    tbl.findall = MagicMock(return_value=[row])

    fake_doc = MagicMock()
    fake_doc.element.body = [para, tbl]
    fake_module = MagicMock()
    fake_module.Document.return_value = fake_doc
    monkeypatch.setitem(sys.modules, "docx", fake_module)

    p = tmp_path / "x.docx"
    p.write_bytes(b"PK")
    out = _extract_docx(p)
    assert "Hello world" in out
    assert "A1" in out


# ---------------------------------------------------------------------------
# _extract_odt
# ---------------------------------------------------------------------------


def test_extract_odt_handles_exception(monkeypatch, tmp_path):
    """ODT load failure is logged and returns empty string."""
    fake_load = MagicMock(side_effect=RuntimeError("bad odt"))

    odf_mod = MagicMock()
    odf_mod.opendocument.load = fake_load
    odf_text = MagicMock()
    odf_text.P = MagicMock()
    monkeypatch.setitem(sys.modules, "odf", odf_mod)
    monkeypatch.setitem(sys.modules, "odf.opendocument", odf_mod.opendocument)
    monkeypatch.setitem(sys.modules, "odf.text", odf_text)

    p = tmp_path / "x.odt"
    p.write_bytes(b"PK")
    assert _extract_odt(p) == ""


def test_extract_odt_paragraphs(monkeypatch, tmp_path):
    """ODT paragraphs are joined with newlines."""
    class FakeNode:
        def __init__(self, data):
            self.data = data

    class FakePara:
        def __init__(self, *nodes):
            self.childNodes = nodes

    fake_doc = MagicMock()
    fake_doc.text.getElementsByType.return_value = [
        FakePara(FakeNode("hello "), FakeNode("world")),
        FakePara(FakeNode("second")),
    ]

    odf_mod = MagicMock()
    odf_mod.opendocument.load.return_value = fake_doc
    odf_text = MagicMock()
    odf_text.P = MagicMock()
    monkeypatch.setitem(sys.modules, "odf", odf_mod)
    monkeypatch.setitem(sys.modules, "odf.opendocument", odf_mod.opendocument)
    monkeypatch.setitem(sys.modules, "odf.text", odf_text)

    p = tmp_path / "x.odt"
    p.write_bytes(b"PK")
    out = _extract_odt(p)
    assert "hello world" in out
    assert "second" in out


# ---------------------------------------------------------------------------
# _bbox_overlap, _split_on_sentences edge cases
# ---------------------------------------------------------------------------


def test_bbox_overlap_inside():
    """An object fully inside the bbox reports overlap."""
    obj = {"x0": 5, "x1": 10, "top": 5, "bottom": 10}
    assert _bbox_overlap(obj, (0, 0, 50, 50)) is True


def test_bbox_overlap_outside():
    """An object outside the bbox reports no overlap."""
    obj = {"x0": 100, "x1": 110, "top": 0, "bottom": 5}
    assert _bbox_overlap(obj, (0, 0, 50, 50)) is False


def test_split_on_sentences_hard_cuts_long_token():
    """A very long single 'sentence' without spaces is hard-cut to max_chars."""
    long_token = "x" * 200
    out = _split_on_sentences(long_token, max_chars=50)
    assert all(len(piece) <= 50 for piece in out)
    assert "".join(out).count("x") == 200


def test_split_on_sentences_short_passthrough():
    """Text under the limit comes back unchanged."""
    assert _split_on_sentences("hi.", max_chars=100) == ["hi."]
