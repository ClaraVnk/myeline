"""Text extraction and chunking utilities shared by the RAG pipeline."""

import re
from pathlib import Path

from app.utils.logger import get_logger


logger = get_logger("text_extraction", subfolder="utils")

# bge-m3 has an 8192-token context window; optimal retrieval chunk ≈ 300-500 tokens.
# At ~4 chars/token for French/English, 1800 chars ≈ 450 tokens — sweet spot.
_MAX_CHUNK_CHARS = 1800
_MIN_CHUNK_CHARS = 40
# Two paragraphs of overlap provide better continuity across chunk boundaries.
_OVERLAP_PARAGRAPHS = 2

# Sentence boundary: period/!/?/… followed by whitespace and an uppercase letter.
# Uses a simple fixed-width look-behind (Python re requires fixed width).
_SENTENCE_SPLIT_RE = re.compile(
    r'(?<=[.!?…])\s+(?=[A-ZÀÁÂÃÄÅÆÇÈÉÊËÌÍÎÏÐÑÒÓÔÕÖÙÚÛÜÝ])',
    re.UNICODE,
)

# A heading is a short line that looks like a title.
_HEADING_RE = re.compile(r'^#{1,6}\s+\S')  # markdown


def extract_text(file_path: Path) -> str:
    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf(file_path)
    if suffix == ".docx":
        return _extract_docx(file_path)
    if suffix == ".odt":
        return _extract_odt(file_path)
    if suffix in (".html", ".htm"):
        return _extract_html(file_path)
    if suffix == ".zip":
        return _extract_zotero_snapshot(file_path)
    return file_path.read_text(encoding="utf-8", errors="replace")


def _extract_html(file_path: Path) -> str:
    """Strip HTML tags and return readable text."""
    try:
        from bs4 import BeautifulSoup
        html = file_path.read_text(encoding="utf-8", errors="replace")
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        return soup.get_text(separator="\n", strip=True)
    except ImportError:
        import html as html_module
        raw = file_path.read_text(encoding="utf-8", errors="replace")
        text = re.sub(r"<[^>]+>", " ", raw)
        return html_module.unescape(text)


# Round 5 A2-H2: Zotero snapshot ZIPs come from arbitrary websites a
# user has saved. A maliciously crafted ZIP can decompress to several
# GB and OOM the worker. Cap the HTML payload at 10 MB BEFORE reading;
# also cap the cumulative size of the whole archive to refuse zip-bomb
# nesting tricks.
_ZOTERO_MAX_HTML_BYTES = 10 * 1024 * 1024  # 10 MB per HTML file
_ZOTERO_MAX_ARCHIVE_BYTES = 50 * 1024 * 1024  # 50 MB total uncompressed


def _extract_zotero_snapshot(file_path: Path) -> str:
    """Extract text from a Zotero snapshot ZIP (HTML archive)."""
    import zipfile
    try:
        with zipfile.ZipFile(file_path) as zf:
            infos = zf.infolist()
            total = sum(i.file_size for i in infos)
            if total > _ZOTERO_MAX_ARCHIVE_BYTES:
                logger.warning(
                    "Zotero snapshot %s exceeds archive cap (%d > %d) — refused",
                    file_path.name, total, _ZOTERO_MAX_ARCHIVE_BYTES,
                )
                return ""
            html_candidates = [
                i for i in infos
                if i.filename.lower().endswith((".html", ".htm"))
            ]
            if not html_candidates:
                return ""
            main = next(
                (i for i in html_candidates if "index" in i.filename.lower()),
                html_candidates[0],
            )
            if main.file_size > _ZOTERO_MAX_HTML_BYTES:
                logger.warning(
                    "Zotero snapshot %s: HTML entry %s exceeds cap (%d > %d)",
                    file_path.name, main.filename, main.file_size, _ZOTERO_MAX_HTML_BYTES,
                )
                return ""
            html_bytes = zf.read(main.filename)
    except (zipfile.BadZipFile, KeyError):
        return ""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_bytes, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        return soup.get_text(separator="\n", strip=True)
    except ImportError:
        import html as html_module
        raw = html_bytes.decode("utf-8", errors="replace")
        text = re.sub(r"<[^>]+>", " ", raw)
        return html_module.unescape(text)


def _extract_pdf(file_path: Path) -> str:
    """
    Extract text from a PDF using pdfplumber (primary) with pdfminer fallback.

    pdfplumber preserves table structure by converting cells to tab-separated
    rows, which keeps values like "Revenu fiscal de référence | 42 000 €" on
    one logical line instead of splitting them across chunks.
    """
    text = _extract_pdf_pdfplumber(file_path)
    if text.strip():
        return text
    logger.warning("pdfplumber returned empty text for %s, falling back to pdfminer", file_path.name)
    return _extract_pdf_pdfminer(file_path)


def _extract_pdf_pdfplumber(file_path: Path) -> str:
    try:
        import pdfplumber
    except ImportError:
        return ""

    pages: list[str] = []
    try:
        with pdfplumber.open(str(file_path)) as pdf:
            for page in pdf.pages:
                parts: list[str] = []

                # Extract tables first; collect bounding boxes to exclude from
                # plain-text extraction so we don't duplicate content.
                table_bboxes: list[tuple] = []
                for table in page.find_tables():
                    bbox = table.bbox
                    table_bboxes.append(bbox)
                    rows = table.extract()
                    if not rows:
                        continue
                    for row in rows:
                        cells = [str(c).strip() if c is not None else "" for c in row]
                        line = "\t".join(cells)
                        if any(c for c in cells):
                            parts.append(line)

                # Extract remaining text, excluding table regions.
                if table_bboxes:
                    remaining = page
                    for bbox in table_bboxes:
                        try:
                            outside = remaining.filter(
                                lambda obj, b=bbox: not _bbox_overlap(obj, b)
                            )
                            remaining = outside
                        except Exception:
                            pass
                    plain = remaining.extract_text(x_tolerance=3, y_tolerance=3) or ""
                else:
                    plain = page.extract_text(x_tolerance=3, y_tolerance=3) or ""

                if plain.strip():
                    parts.append(plain.strip())

                if parts:
                    pages.append("\n".join(parts))

        return "\n\n".join(pages)
    except Exception as e:
        logger.error("pdfplumber extraction failed for %s: %s", file_path.name, e)
        return ""


def _bbox_overlap(obj: dict, bbox: tuple) -> bool:
    """Return True if a pdfplumber object overlaps with the given bounding box."""
    x0, top, x1, bottom = bbox
    ox0 = obj.get("x0", 0)
    ox1 = obj.get("x1", 0)
    otop = obj.get("top", 0)
    obottom = obj.get("bottom", 0)
    return ox0 < x1 and ox1 > x0 and otop < bottom and obottom > top


def _extract_pdf_pdfminer(file_path: Path) -> str:
    try:
        from pdfminer.high_level import extract_text as pdf_extract
        return pdf_extract(str(file_path)) or ""
    except Exception as e:
        logger.error("pdfminer extraction failed for %s: %s", file_path.name, e)
        return ""


def _extract_docx(file_path: Path) -> str:
    from docx import Document
    try:
        doc = Document(str(file_path))
        parts: list[str] = []
        for block in doc.element.body:
            tag = block.tag.split("}")[-1] if "}" in block.tag else block.tag
            if tag == "p":
                text = "".join(n.text or "" for n in block.iter() if n.text)
                if text.strip():
                    # Preserve heading markers for downstream heading detection.
                    style = block.get(
                        "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}pPr"
                    )
                    parts.append(text.strip())
            elif tag == "tbl":
                for row in block.findall(".//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}tr"):
                    cells = []
                    for cell in row.findall(".//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}tc"):
                        cell_text = "".join(n.text or "" for n in cell.iter() if n.text)
                        cells.append(cell_text.strip())
                    if any(cells):
                        parts.append("\t".join(cells))
        return "\n".join(parts)
    except Exception as e:
        logger.error("DOCX extraction failed for %s: %s", file_path.name, e)
        return ""


def _extract_odt(file_path: Path) -> str:
    from odf.opendocument import load as odf_load
    from odf.text import P
    try:
        doc = odf_load(str(file_path))
        paragraphs = doc.text.getElementsByType(P)
        return "\n".join(
            "".join(node.data for node in p.childNodes if hasattr(node, "data"))
            for p in paragraphs
        )
    except Exception as e:
        logger.error("ODT extraction failed for %s: %s", file_path.name, e)
        return ""


def _is_heading(line: str) -> bool:
    """Heuristic heading detector for unstructured text.

    A line is treated as a heading when it is short, doesn't end with
    sentence-final punctuation, and looks title-cased or is all-caps.
    Markdown headings (# …) are always recognised.
    """
    stripped = line.strip()
    if not stripped:
        return False
    if _HEADING_RE.match(stripped):
        return True
    # Reject anything longer than ~100 chars — too long to be a heading.
    if len(stripped) > 100:
        return False
    # Reject lines ending with normal sentence punctuation.
    if stripped[-1] in '.,:;()[]{}':
        return False
    words = stripped.split()
    if len(words) < 2:
        # Single-word lines that look like labels (capitalised, short).
        return len(stripped) < 40 and stripped[0].isupper()
    uppercase_starts = sum(1 for w in words if w and w[0].isupper())
    ratio = uppercase_starts / len(words)
    # Allow fully all-caps headings or title-cased lines (≥ 60 % words capitalised).
    return ratio >= 0.6 or stripped.isupper()


def _split_on_sentences(text: str, max_chars: int) -> list[str]:
    """Split *text* into pieces ≤ max_chars, breaking on sentence boundaries.

    Falls back to whitespace, then hard-cuts only as a last resort.
    """
    if len(text) <= max_chars:
        return [text]

    parts: list[str] = []
    current = ""
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        if not sentence:
            continue
        candidate = (current + " " + sentence).strip() if current else sentence
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                parts.append(current)
            # If the sentence itself is too long, fall back to whitespace then hard cut.
            while len(sentence) > max_chars:
                cut = sentence.rfind(" ", 0, max_chars)
                if cut <= 0:
                    cut = max_chars
                parts.append(sentence[:cut].strip())
                sentence = sentence[cut:].strip()
            current = sentence
    if current:
        parts.append(current)
    return parts or [text[:max_chars]]


def chunk_text(text: str) -> list[str]:
    """
    Heading-aware, sentence-boundary paragraph chunker.

    1. Split on blank lines to get paragraphs.
    2. Detect heading lines; carry the last heading as context prefix into
       each subsequent chunk so retrieval embeddings include section context.
    3. Merge short paragraphs into chunks up to _MAX_CHUNK_CHARS.
    4. Split overlong paragraphs on sentence boundaries (not raw whitespace).
    5. Carry _OVERLAP_PARAGRAPHS of overlap into the next chunk for context
       continuity across chunk boundaries.
    """
    text = text.strip()
    if not text:
        return []

    # ── Step 1: split on blank lines ────────────────────────────────────────
    raw_paras = [p.strip() for p in re.split(r"\n{2,}", text)]

    # ── Step 2 & 4: detect headings, split overlong paragraphs ──────────────
    paragraphs: list[str] = []   # (text, is_heading)
    paragraph_is_heading: list[bool] = []
    last_heading: str = ""

    for para in raw_paras:
        if not para:
            continue
        # Single-line paragraph — check for heading.
        lines = para.splitlines()
        if len(lines) == 1 and _is_heading(para):
            # Strip markdown # prefix for cleaner display.
            clean = re.sub(r'^#{1,6}\s+', '', para).strip()
            last_heading = clean
            paragraphs.append(clean)
            paragraph_is_heading.append(True)
            continue

        # Multi-line or body paragraph: split on sentences if overlong.
        if len(para) > _MAX_CHUNK_CHARS:
            for piece in _split_on_sentences(para, _MAX_CHUNK_CHARS):
                if piece:
                    paragraphs.append(piece)
                    paragraph_is_heading.append(False)
        else:
            paragraphs.append(para)
            paragraph_is_heading.append(False)

    if not paragraphs:
        return []

    # ── Step 3 & 5: merge into chunks with overlap ───────────────────────────
    chunks: list[str] = []
    bucket: list[str] = []
    bucket_len = 0
    # Track the heading that was active when this bucket started.
    bucket_heading: str = ""

    def _flush(bucket: list[str], heading: str) -> str:
        body = "\n\n".join(bucket)
        if heading and not body.startswith(heading):
            return f"{heading}\n\n{body}"
        return body

    current_heading: str = ""

    for para, is_h in zip(paragraphs, paragraph_is_heading):
        if is_h:
            current_heading = para
            # Headings do not go into the text bucket; they annotate the next chunk.
            continue

        para_len = len(para)

        if bucket and bucket_len + para_len + 2 > _MAX_CHUNK_CHARS:
            chunks.append(_flush(bucket, bucket_heading))
            # Overlap: carry last N body paragraphs (no heading re-emission).
            bucket = bucket[-_OVERLAP_PARAGRAPHS:] if _OVERLAP_PARAGRAPHS > 0 else []
            bucket_len = sum(len(p) for p in bucket) + 2 * max(len(bucket) - 1, 0)
            bucket_heading = current_heading  # new chunk gets current heading

        if not bucket:
            bucket_heading = current_heading

        bucket.append(para)
        bucket_len += para_len + 2

    if bucket:
        chunks.append(_flush(bucket, bucket_heading))

    return [c for c in chunks if len(c.strip()) >= _MIN_CHUNK_CHARS]
