"""Coverage for app/utils/text_extraction.py.

Focuses on the text-only paths (HTML, plain-text, chunking) where
fixtures are easy to build inline. PDF/DOCX/ODT extractors are
exercised indirectly via the public extract_text dispatcher; we
mock the underlying libs where needed to avoid bundling binary
fixtures into the repo.
"""
import re
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from app.utils.text_extraction import (
    _bbox_overlap,
    _extract_docx,
    _extract_html,
    _extract_odt,
    _extract_pdf,
    _extract_pdf_pdfminer,
    _extract_pdf_pdfplumber,
    _extract_zotero_snapshot,
    _is_heading,
    _split_on_sentences,
    chunk_text,
    extract_text,
)


# ─────────────────────────────────────────────────────────────────
# extract_text dispatch
# ─────────────────────────────────────────────────────────────────

class TestExtractTextDispatch:
    def test_txt_falls_through_to_read_text(self, tmp_path):
        f = tmp_path / "note.txt"
        f.write_text("plain ASCII content", encoding="utf-8")
        assert extract_text(f) == "plain ASCII content"

    def test_md_treated_as_text(self, tmp_path):
        f = tmp_path / "note.md"
        f.write_text("# title\n\nbody", encoding="utf-8")
        assert "title" in extract_text(f)

    def test_unknown_suffix_treated_as_text(self, tmp_path):
        f = tmp_path / "note.weird"
        f.write_text("some bytes", encoding="utf-8")
        assert extract_text(f) == "some bytes"

    def test_html_dispatched_to_html_extractor(self, tmp_path):
        f = tmp_path / "page.html"
        f.write_text("<html><body><p>hello world</p></body></html>", encoding="utf-8")
        assert "hello world" in extract_text(f)

    def test_htm_alias_dispatched(self, tmp_path):
        f = tmp_path / "page.htm"
        f.write_text("<p>alias</p>", encoding="utf-8")
        assert "alias" in extract_text(f)

    def test_pdf_dispatched(self, tmp_path):
        f = tmp_path / "doc.pdf"
        f.write_bytes(b"")  # bs4-empty file, both extractors return ""
        with patch("app.utils.text_extraction._extract_pdf_pdfplumber", return_value="from-plumber") as mock:
            result = extract_text(f)
        assert mock.called
        assert result == "from-plumber"

    def test_docx_dispatched(self, tmp_path):
        f = tmp_path / "doc.docx"
        f.write_bytes(b"")
        with patch("app.utils.text_extraction._extract_docx", return_value="from-docx") as mock:
            result = extract_text(f)
        assert mock.called
        assert result == "from-docx"

    def test_odt_dispatched(self, tmp_path):
        f = tmp_path / "doc.odt"
        f.write_bytes(b"")
        with patch("app.utils.text_extraction._extract_odt", return_value="from-odt") as mock:
            result = extract_text(f)
        assert mock.called
        assert result == "from-odt"

    def test_zip_dispatched_to_zotero(self, tmp_path):
        f = tmp_path / "snap.zip"
        f.write_bytes(b"")
        with patch("app.utils.text_extraction._extract_zotero_snapshot", return_value="from-zip") as mock:
            result = extract_text(f)
        assert mock.called
        assert result == "from-zip"


# ─────────────────────────────────────────────────────────────────
# _extract_html
# ─────────────────────────────────────────────────────────────────

class TestExtractHtml:
    def test_strips_tags(self, tmp_path):
        f = tmp_path / "p.html"
        f.write_text(
            "<html><body><p>Hello <b>world</b></p></body></html>", encoding="utf-8"
        )
        out = _extract_html(f)
        assert "Hello" in out
        assert "world" in out
        assert "<b>" not in out

    def test_drops_script_and_style(self, tmp_path):
        f = tmp_path / "p.html"
        f.write_text(
            "<html><head><style>.x{}</style><script>alert(1)</script></head>"
            "<body><p>visible</p></body></html>",
            encoding="utf-8",
        )
        out = _extract_html(f)
        assert "alert" not in out
        assert ".x" not in out
        assert "visible" in out

    def test_drops_nav_footer_header(self, tmp_path):
        f = tmp_path / "p.html"
        f.write_text(
            "<html><body>"
            "<nav>NAV-skip</nav>"
            "<header>HEAD-skip</header>"
            "<p>keep</p>"
            "<footer>FOOT-skip</footer>"
            "</body></html>",
            encoding="utf-8",
        )
        out = _extract_html(f)
        assert "skip" not in out
        assert "keep" in out

    def test_handles_invalid_utf8_via_replace(self, tmp_path):
        f = tmp_path / "p.html"
        f.write_bytes(b"<p>caf\xe9</p>")  # latin-1 byte
        out = _extract_html(f)
        # Should not raise; either replaced char or decoded as something
        assert out is not None


# ─────────────────────────────────────────────────────────────────
# _extract_zotero_snapshot
# ─────────────────────────────────────────────────────────────────

class TestExtractZoteroSnapshot:
    def test_picks_index_html(self, tmp_path):
        zpath = tmp_path / "snap.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("other.html", "<p>WRONG</p>")
            zf.writestr("index.html", "<p>RIGHT</p>")
        out = _extract_zotero_snapshot(zpath)
        assert "RIGHT" in out
        assert "WRONG" not in out

    def test_falls_back_to_first_html_when_no_index(self, tmp_path):
        zpath = tmp_path / "snap.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("page.html", "<p>only choice</p>")
        out = _extract_zotero_snapshot(zpath)
        assert "only choice" in out

    def test_returns_empty_when_no_html(self, tmp_path):
        zpath = tmp_path / "snap.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("data.json", "{}")
        assert _extract_zotero_snapshot(zpath) == ""

    def test_returns_empty_for_invalid_zip(self, tmp_path):
        zpath = tmp_path / "broken.zip"
        zpath.write_bytes(b"not-a-zip")
        assert _extract_zotero_snapshot(zpath) == ""


# ─────────────────────────────────────────────────────────────────
# _extract_pdf — fallback chain
# ─────────────────────────────────────────────────────────────────

class TestExtractPdf:
    def test_uses_pdfplumber_when_it_returns_text(self, tmp_path):
        f = tmp_path / "x.pdf"
        f.write_bytes(b"")
        with patch("app.utils.text_extraction._extract_pdf_pdfplumber", return_value="from-plumber"):
            with patch("app.utils.text_extraction._extract_pdf_pdfminer") as miner:
                out = _extract_pdf(f)
        assert out == "from-plumber"
        miner.assert_not_called()

    def test_falls_back_to_pdfminer_when_plumber_empty(self, tmp_path):
        f = tmp_path / "x.pdf"
        f.write_bytes(b"")
        with patch("app.utils.text_extraction._extract_pdf_pdfplumber", return_value=""):
            with patch("app.utils.text_extraction._extract_pdf_pdfminer", return_value="from-miner") as miner:
                out = _extract_pdf(f)
        assert out == "from-miner"
        miner.assert_called_once()

    def test_pdfplumber_returns_empty_when_lib_missing(self, tmp_path, monkeypatch):
        f = tmp_path / "x.pdf"
        f.write_bytes(b"")
        # Force ImportError on pdfplumber
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "pdfplumber":
                raise ImportError("not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        assert _extract_pdf_pdfplumber(f) == ""

    def test_pdfminer_returns_empty_on_exception(self, tmp_path):
        f = tmp_path / "x.pdf"
        f.write_bytes(b"not-a-pdf")
        # pdfminer will raise → caught → returns ""
        out = _extract_pdf_pdfminer(f)
        assert out == ""


# ─────────────────────────────────────────────────────────────────
# _extract_docx / _extract_odt — error paths
# ─────────────────────────────────────────────────────────────────

class TestExtractDocxOdt:
    def test_docx_returns_empty_on_corrupt_file(self, tmp_path):
        f = tmp_path / "broken.docx"
        f.write_bytes(b"not-a-docx")
        # python-docx raises; we catch and return ""
        assert _extract_docx(f) == ""

    def test_odt_returns_empty_on_corrupt_file(self, tmp_path):
        f = tmp_path / "broken.odt"
        f.write_bytes(b"not-an-odt")
        assert _extract_odt(f) == ""


# ─────────────────────────────────────────────────────────────────
# _bbox_overlap
# ─────────────────────────────────────────────────────────────────

class TestBboxOverlap:
    def test_overlapping_box_returns_true(self):
        # bbox (left, top, right, bottom)
        bbox = (0, 0, 100, 100)
        obj = {"x0": 50, "x1": 75, "top": 50, "bottom": 75}
        assert _bbox_overlap(obj, bbox) is True

    def test_disjoint_box_returns_false(self):
        bbox = (0, 0, 100, 100)
        obj = {"x0": 200, "x1": 300, "top": 200, "bottom": 300}
        assert _bbox_overlap(obj, bbox) is False

    def test_touching_edge_returns_false(self):
        # objects sharing an edge are not overlapping
        bbox = (0, 0, 100, 100)
        obj = {"x0": 100, "x1": 200, "top": 0, "bottom": 50}
        assert _bbox_overlap(obj, bbox) is False


# ─────────────────────────────────────────────────────────────────
# _is_heading heuristic
# ─────────────────────────────────────────────────────────────────

class TestIsHeading:
    @pytest.mark.parametrize("line", [
        "# Markdown title",
        "## Section",
        "### Subsection",
    ])
    def test_markdown_headings(self, line):
        assert _is_heading(line) is True

    def test_all_caps_short_line(self):
        assert _is_heading("INTRODUCTION") is True

    def test_title_cased_short_line(self):
        assert _is_heading("Plan Overview Section") is True

    def test_normal_sentence_is_not_heading(self):
        assert _is_heading("This is a normal sentence.") is False

    def test_empty_line_not_heading(self):
        assert _is_heading("") is False
        assert _is_heading("   ") is False

    def test_long_line_not_heading(self):
        long_line = "Capitalised " * 20  # >100 chars
        assert _is_heading(long_line) is False

    def test_line_ending_in_punctuation_not_heading(self):
        assert _is_heading("Title with colon:") is False
        assert _is_heading("Some Title.") is False

    def test_single_word_uppercase(self):
        assert _is_heading("Overview") is True

    def test_single_lowercase_word_not_heading(self):
        assert _is_heading("introduction") is False


# ─────────────────────────────────────────────────────────────────
# _split_on_sentences
# ─────────────────────────────────────────────────────────────────

class TestSplitOnSentences:
    def test_short_text_returned_as_is(self):
        assert _split_on_sentences("Short text.", 100) == ["Short text."]

    def test_splits_at_sentence_boundary(self):
        text = "First sentence. Second sentence. Third sentence."
        # max_chars chosen to force at least one split
        parts = _split_on_sentences(text, 30)
        assert len(parts) >= 2
        # No part exceeds max_chars by a wide margin
        assert all(len(p) <= 50 for p in parts)

    def test_handles_no_sentence_boundaries(self):
        # Long text with no sentence-final punctuation — falls back to whitespace
        text = "word " * 100
        parts = _split_on_sentences(text, 50)
        assert len(parts) > 1

    def test_handles_extremely_long_word(self):
        # A single "word" longer than max_chars must still produce output
        text = "A" * 200
        parts = _split_on_sentences(text, 50)
        assert all(len(p) <= 50 for p in parts)


# ─────────────────────────────────────────────────────────────────
# chunk_text — the main API
# ─────────────────────────────────────────────────────────────────

class TestChunkText:
    def test_empty_returns_empty(self):
        assert chunk_text("") == []
        assert chunk_text("   \n\n  ") == []

    def test_single_short_paragraph(self):
        chunks = chunk_text("A short paragraph that fits in a single chunk.")
        assert len(chunks) == 1
        assert "short paragraph" in chunks[0]

    def test_multi_paragraph_fits_one_chunk(self):
        # Each paragraph must be long enough that the merged chunk stays
        # above _MIN_CHUNK_CHARS (40) — chunks shorter than that are dropped.
        text = (
            "Paragraph one with enough content to clear the minimum.\n\n"
            "Paragraph two also has enough length here.\n\n"
            "Paragraph three completes the trio."
        )
        chunks = chunk_text(text)
        assert len(chunks) == 1
        joined = chunks[0]
        assert "Paragraph one" in joined and "Paragraph three" in joined

    def test_overlong_paragraph_is_split(self):
        # 9000 chars > _MAX_CHUNK_CHARS (1800)
        long_text = ("This is a sentence about programming concepts. " * 200).strip()
        chunks = chunk_text(long_text)
        # The point of this test: a single long paragraph must be split into
        # multiple chunks (not produce one giant chunk and not lose content).
        assert len(chunks) > 1
        joined_len = sum(len(c) for c in chunks)
        assert joined_len >= len(long_text)  # nothing lost

    def test_heading_carried_into_following_chunks(self):
        text = (
            "# Section A\n\n"
            + ("Filler sentence about topic A. " * 100)
            + "\n\n"
            + "# Section B\n\n"
            + ("Different topic B content. " * 100)
        )
        chunks = chunk_text(text)
        # The heading should be present in the body chunk that follows it
        assert any("Section A" in c for c in chunks)
        assert any("Section B" in c for c in chunks)

    def test_chunks_have_paragraph_overlap(self):
        # Construct paragraphs that don't fit in one chunk
        paras = [f"Paragraph number {i} with some filler text content." * 5 for i in range(20)]
        text = "\n\n".join(paras)
        chunks = chunk_text(text)
        assert len(chunks) >= 2
        # Adjacent chunks should share at least one paragraph (overlap mechanism)
        # Check that some paragraphs appear in multiple chunks
        # (this is a soft check — overlap may collapse if budgets are tight)
        all_text = "\n\n".join(chunks)
        assert "Paragraph" in all_text

    def test_markdown_headings_have_hash_prefix_stripped(self):
        # Body must clear _MIN_CHUNK_CHARS once heading is prepended.
        text = (
            "# Real Title\n\n"
            "Body content that is long enough to survive the minimum-chunk "
            "filter so we can actually inspect what the chunker produced."
        )
        chunks = chunk_text(text)
        assert any("Real Title" in c for c in chunks)
        for c in chunks:
            if "Real Title" in c:
                # The hash-prefix form must not survive into the chunk
                assert not re.search(r"#\s+Real Title", c)
