"""Bank PDF Core Engine, PyMuPDF Ephemeral Password Handling, and Watermark Stripping."""

from __future__ import annotations

import inspect
import io
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple, Union
import pymupdf

from billapp.importers.base import FileProbe


class PDFError(Exception):
    """Base exception for PDF operations."""


class PDFPasswordRequiredError(PDFError):
    """Raised when an encrypted PDF is supplied without a password."""


class PDFInvalidPasswordError(PDFError):
    """Raised when an incorrect password is supplied for an encrypted PDF."""


class PDFCorruptedError(PDFError):
    """Raised when PDF bytes cannot be opened or parsed."""


# Backward compatibility alias
PasswordRequiredError = PDFPasswordRequiredError


class PDFDocumentSession:
    """Encapsulates an open PyMuPDF document with ephemeral lifecycle management.
    
    Zero disk persistence: passwords and decryptions reside strictly in memory.
    """

    def __init__(self, doc: pymupdf.Document, was_encrypted: bool = False) -> None:
        self._doc: Optional[pymupdf.Document] = doc
        self.was_encrypted: bool = was_encrypted

    @property
    def doc(self) -> pymupdf.Document:
        if self._doc is None or self._doc.is_closed:
            raise PDFCorruptedError("PDF document has already been closed.")
        return self._doc

    def __enter__(self) -> PDFDocumentSession:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def __getitem__(self, index: int) -> pymupdf.Page:
        return self.doc[index]

    def __len__(self) -> int:
        return len(self.doc)

    def __iter__(self) -> Generator[pymupdf.Page, None, None]:
        for page in self.doc:
            yield page

    def close(self) -> None:
        if self._doc is not None and not self._doc.is_closed:
            self._doc.close()
            self._doc = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.doc, name)


def open_pdf_document(
    source: Union[FileProbe, bytes, Path, str],
    password: Optional[str] = None,
    file_name: Optional[str] = None,
) -> PDFDocumentSession:
    """Open and authenticate an in-memory or on-disk PDF document.

    Zero disk persistence: password is never saved to database, settings, or DPAPI.
    Ephemeral in-memory authentication only.
    """
    if isinstance(source, FileProbe):
        content = source.content
        pwd = password if password is not None else source.password
        fname = file_name or source.file_name
    elif isinstance(source, bytes):
        content = source
        pwd = password
        fname = file_name or "statement.pdf"
    elif isinstance(source, (Path, str)):
        path = Path(source)
        fname = file_name or path.name
        pwd = password
        try:
            content = path.read_bytes()
        except Exception as exc:
            raise PDFCorruptedError(f"Failed to read PDF file '{fname}': {exc}") from exc
    else:
        raise PDFCorruptedError(f"Invalid PDF source type: {type(source)}")

    if not content:
        raise PDFCorruptedError(f"Cannot open empty PDF stream for '{fname}'.")

    try:
        doc = pymupdf.open(stream=content, filetype="pdf")
    except (pymupdf.FileDataError, pymupdf.EmptyFileError, Exception) as exc:
        raise PDFCorruptedError(f"Failed to open PDF stream for '{fname}': {exc}") from exc

    was_encrypted = bool(doc.is_encrypted or doc.needs_pass)
    if was_encrypted:
        if not pwd:
            doc.close()
            raise PDFPasswordRequiredError(f"PDF statement '{fname}' is encrypted. Password is required.")
        auth_result = doc.authenticate(pwd)
        if auth_result == 0:
            doc.close()
            raise PDFInvalidPasswordError(f"Invalid password supplied for encrypted PDF '{fname}'.")

    return PDFDocumentSession(doc, was_encrypted=was_encrypted)


class WatermarkFilter:
    """Detects and filters out watermark text spans based on line direction vectors."""

    @staticmethod
    def is_diagonal_watermark(dir_vec: Optional[Tuple[float, float]], threshold: float = 0.15) -> bool:
        """Return True if direction vector indicates diagonal watermark (e.g. 30°, 45°, 60°)."""
        if not dir_vec or len(dir_vec) < 2:
            return False
        dx, dy = abs(dir_vec[0]), abs(dir_vec[1])
        # Diagonal lines deviate significantly from both horizontal (1,0) and vertical (0,-1)/(0,1)
        return dx > threshold and dy > threshold

    @classmethod
    def extract_clean_text_spans(
        cls,
        page: pymupdf.Page,
        strip_watermark: bool = True,
        threshold: float = 0.15,
    ) -> List[Dict[str, Any]]:
        """Extract clean text spans from page, transformed into visual coordinate space."""
        d = page.get_text("dict")
        rot_mat = page.rotation_matrix
        clean_spans: List[Dict[str, Any]] = []

        for block in d.get("blocks", []):
            if "lines" not in block:
                continue
            for line in block["lines"]:
                dir_vec = line.get("dir", (1.0, 0.0))
                if strip_watermark and cls.is_diagonal_watermark(dir_vec, threshold=threshold):
                    continue
                for span in line.get("spans", []):
                    text = span.get("text", "")
                    if not text.strip():
                        continue
                    vis_box = pymupdf.Rect(span["bbox"]) * rot_mat
                    center = ((vis_box.x0 + vis_box.x1) / 2.0, (vis_box.y0 + vis_box.y1) / 2.0)
                    clean_spans.append({
                        "text": text,
                        "bbox": vis_box,
                        "orig_bbox": span["bbox"],
                        "center": center,
                        "font": span.get("font"),
                        "size": span.get("size"),
                        "color": span.get("color"),
                        "flags": span.get("flags"),
                    })
        return clean_spans


def extract_clean_text_spans(
    page: pymupdf.Page,
    strip_watermark: bool = True,
    threshold: float = 0.15,
) -> List[Dict[str, Any]]:
    """Extract clean text spans from a PDF page, filtering out diagonal watermarks."""
    return WatermarkFilter.extract_clean_text_spans(page, strip_watermark=strip_watermark, threshold=threshold)


def _is_point_in_rect(
    pt: Tuple[float, float],
    rect: Union[pymupdf.Rect, Tuple[float, float, float, float]],
    tol: float = 1.0,
) -> bool:
    """Check if point pt falls inside bounding rect with optional tolerance."""
    r_x0 = rect.x0 if hasattr(rect, "x0") else rect[0]
    r_y0 = rect.y0 if hasattr(rect, "y0") else rect[1]
    r_x1 = rect.x1 if hasattr(rect, "x1") else rect[2]
    r_y1 = rect.y1 if hasattr(rect, "y1") else rect[3]
    return (r_x0 - tol <= pt[0] <= r_x1 + tol) and (r_y0 - tol <= pt[1] <= r_y1 + tol)


def _find_tables_supports_use_layout() -> bool:
    """Return True if PyMuPDF's table finder exposes ``use_layout``."""
    try:
        from pymupdf import table as pymupdf_table

        return "use_layout" in inspect.signature(pymupdf_table.find_tables).parameters
    except Exception:
        return False


_HAS_FIND_TABLES_USE_LAYOUT = _find_tables_supports_use_layout()


def _find_tables(page: pymupdf.Page):
    """Detect grid tables via PyMuPDF without layout-box pruning.

    When ``pymupdf_layout`` is active, PyMuPDF defaults to ``use_layout=True``
    and discards vector-grid tables whose bounding box diverges (IoU < 0.6) from
    a layout box stretched by diagonal watermarks or adjacent page footers.
    """
    if _HAS_FIND_TABLES_USE_LAYOUT:
        return page.find_tables(use_layout=False)
    return page.find_tables()


def extract_tables_strategy_a(page: pymupdf.Page, tabs: Optional[Any] = None) -> List[List[List[str]]]:
    """Strategy A: Direct PyMuPDF find_tables() table extraction."""
    tabs = tabs if tabs is not None else _find_tables(page)
    tables: List[List[List[str]]] = []
    for tab in tabs.tables:
        extracted = tab.extract()
        if extracted:
            # Normalize None cells to empty string
            clean_table = [[c.strip() if c else "" for c in row] for row in extracted]
            tables.append(clean_table)
    return tables


def extract_tables_hybrid(
    page: pymupdf.Page,
    strip_watermark: bool = True,
    tabs: Optional[Any] = None,
) -> List[List[List[str]]]:
    """Hybrid Strategy: Uses find_tables() for cell boundaries and clean text spans for cell content."""
    tabs = tabs if tabs is not None else _find_tables(page)
    if not tabs.tables:
        return []

    clean_spans = extract_clean_text_spans(page, strip_watermark=strip_watermark)
    tables: List[List[List[str]]] = []

    for tab in tabs.tables:
        table_grid: List[List[str]] = []
        for r_idx in range(tab.row_count):
            row_cells = tab.rows[r_idx].cells
            row_data: List[str] = []
            for c_idx in range(tab.col_count):
                cell_box = row_cells[c_idx]
                if cell_box is None:
                    row_data.append("")
                    continue
                matched = [s for s in clean_spans if _is_point_in_rect(s["center"], cell_box)]
                matched.sort(key=lambda s: (s["bbox"].y0, s["bbox"].x0))
                # Group text spans by line
                lines: List[str] = []
                curr_line: List[str] = []
                curr_y: Optional[float] = None
                for s in matched:
                    sy = (s["bbox"].y0 + s["bbox"].y1) / 2.0
                    if curr_y is None or abs(sy - curr_y) < 3.5:
                        curr_line.append(s["text"])
                        curr_y = sy
                    else:
                        lines.append(" ".join(curr_line))
                        curr_line = [s["text"]]
                        curr_y = sy
                if curr_line:
                    lines.append(" ".join(curr_line))
                cell_text = "\n".join(lines).strip()
                row_data.append(cell_text)
            table_grid.append(row_data)
        tables.append(table_grid)

    return tables


def extract_tables_strategy_b(
    page: pymupdf.Page,
    strip_watermark: bool = True,
    y_tol: float = 6.0,
) -> List[List[List[str]]]:
    """Strategy B: Coordinate-based span clustering for borderless flow tables."""
    spans = extract_clean_text_spans(page, strip_watermark=strip_watermark)
    if not spans:
        return []

    # Sort spans primarily by visual y0, then visual x0
    spans.sort(key=lambda s: (s["bbox"].y0, s["bbox"].x0))

    # Cluster rows by visual y midpoint
    clustered_rows: List[List[Dict[str, Any]]] = []
    curr_row: List[Dict[str, Any]] = []
    curr_y: Optional[float] = None

    for s in spans:
        y_mid = (s["bbox"].y0 + s["bbox"].y1) / 2.0
        if curr_y is None:
            curr_y = y_mid
            curr_row.append(s)
        elif abs(y_mid - curr_y) < y_tol:
            curr_row.append(s)
        else:
            curr_row.sort(key=lambda item: item["bbox"].x0)
            clustered_rows.append(curr_row)
            curr_row = [s]
            curr_y = y_mid
    if curr_row:
        curr_row.sort(key=lambda item: item["bbox"].x0)
        clustered_rows.append(curr_row)

    # Format into list of strings
    formatted_table: List[List[str]] = []
    for r in clustered_rows:
        row_strings = [s["text"].strip() for s in r if s["text"].strip()]
        if row_strings:
            formatted_table.append(row_strings)

    return [formatted_table] if formatted_table else []


def extract_pdf_tables(
    source: Union[FileProbe, bytes, Path, str],
    password: Optional[str] = None,
    strategy: str = "auto",
) -> List[List[List[str]]]:
    """Extract tables across all pages in a PDF document using the specified strategy.
    
    strategy in ['auto', 'hybrid', 'strategy_a', 'strategy_b']
    """
    with open_pdf_document(source, password=password) as doc:
        all_tables: List[List[List[str]]] = []
        for page in doc:
            if strategy == "hybrid":
                tables = extract_tables_hybrid(page, strip_watermark=True)
            elif strategy == "strategy_a":
                tables = extract_tables_strategy_a(page)
            elif strategy == "strategy_b":
                tables = extract_tables_strategy_b(page, strip_watermark=True)
            else:  # auto
                # Check for diagonal watermark spans
                d = page.get_text("dict")
                has_watermark = False
                for block in d.get("blocks", []):
                    for line in block.get("lines", []):
                        if WatermarkFilter.is_diagonal_watermark(line.get("dir")):
                            has_watermark = True
                            break
                    if has_watermark:
                        break

                tabs = _find_tables(page)
                if has_watermark and tabs.tables:
                    tables = extract_tables_hybrid(page, strip_watermark=True, tabs=tabs)
                elif tabs.tables:
                    tables = extract_tables_strategy_a(page, tabs=tabs)
                else:
                    tables = extract_tables_strategy_b(page, strip_watermark=True)

            all_tables.extend(tables)
        return all_tables


def _cluster_spans_to_rows(
    spans: List[Dict[str, Any]],
    y_tol: float = 6.0,
    cell_gap: float = 18.0,
) -> List[List[str]]:
    """Group visual text spans into rows and split distant horizontal spans into cells."""
    if not spans:
        return []
    ordered = sorted(spans, key=lambda s: (s["bbox"].y0, s["bbox"].x0))
    clustered: List[List[Dict[str, Any]]] = []
    curr_row: List[Dict[str, Any]] = []
    curr_y: Optional[float] = None

    for s in ordered:
        y_mid = (s["bbox"].y0 + s["bbox"].y1) / 2.0
        if curr_y is None or abs(y_mid - curr_y) < y_tol:
            if curr_y is None:
                curr_y = y_mid
            curr_row.append(s)
        else:
            curr_row.sort(key=lambda item: item["bbox"].x0)
            clustered.append(curr_row)
            curr_row = [s]
            curr_y = y_mid
    if curr_row:
        curr_row.sort(key=lambda item: item["bbox"].x0)
        clustered.append(curr_row)

    rows: List[List[str]] = []
    for row_spans in clustered:
        cells: List[str] = []
        buf: List[str] = []
        prev_x1: Optional[float] = None
        for sp in row_spans:
            txt = sp["text"].strip()
            if not txt:
                continue
            if prev_x1 is not None and (sp["bbox"].x0 - prev_x1) > cell_gap and buf:
                cells.append(" ".join(buf))
                buf = [txt]
            else:
                buf.append(txt)
            prev_x1 = max(prev_x1 or sp["bbox"].x1, sp["bbox"].x1)
        if buf:
            cells.append(" ".join(buf))
        if cells:
            rows.append(cells)
    return rows


def extract_pdf_preamble_lines(
    source: Union[FileProbe, bytes, Path, str],
    password: Optional[str] = None,
    max_lines: int = 30,
) -> List[List[str]]:
    """Extract clean horizontal top lines from page 1 of a PDF (outside or above tables)."""
    with open_pdf_document(source, password=password) as doc:
        if len(doc) == 0:
            return []
        spans = extract_clean_text_spans(doc[0], strip_watermark=True)
        rows = _cluster_spans_to_rows(spans)
        return rows[:max_lines]


def extract_pdf_statement_lines(
    source: Union[FileProbe, bytes, Path, str],
    password: Optional[str] = None,
) -> List[str]:
    """Extract watermark-free reading-order lines across all PDF pages for full parsing."""
    lines: List[str] = []
    with open_pdf_document(source, password=password) as doc:
        for page in doc:
            clean_spans = extract_clean_text_spans(page, strip_watermark=True)
            tabs = _find_tables(page)
            table_objs = list(tabs.tables) if tabs and tabs.tables else []
            if table_objs:
                rot_mat = page.rotation_matrix
                # Preserve visual text outside tables (e.g. account headers above or totals below)
                table_rects = [pymupdf.Rect(t.bbox) * rot_mat for t in table_objs]
                first_top = min(r.y0 for r in table_rects)
                last_bottom = max(r.y1 for r in table_rects)
                above_spans = [s for s in clean_spans if s["center"][1] < first_top - 2.0]
                below_spans = [s for s in clean_spans if s["center"][1] > last_bottom + 2.0]
                for row in _cluster_spans_to_rows(above_spans):
                    joined = " | ".join(c for c in row if c)
                    if joined:
                        lines.append(joined)

                # Extract structured table content with watermark spans filtered
                d = page.get_text("dict")
                has_wm = any(
                    WatermarkFilter.is_diagonal_watermark(line.get("dir"))
                    for blk in d.get("blocks", [])
                    for line in blk.get("lines", [])
                )
                extracted_tables = (
                    extract_tables_hybrid(page, strip_watermark=True, tabs=tabs)
                    if has_wm
                    else extract_tables_strategy_a(page, tabs=tabs)
                )
                for tbl in extracted_tables:
                    for row in tbl:
                        joined = " | ".join(c.replace("\n", " ").strip() for c in row if c and c.strip())
                        if joined:
                            lines.append(joined)

                for row in _cluster_spans_to_rows(below_spans):
                    joined = " | ".join(c for c in row if c)
                    if joined:
                        lines.append(joined)
            else:
                for row in _cluster_spans_to_rows(clean_spans):
                    joined = " | ".join(c for c in row if c)
                    if joined:
                        lines.append(joined)
    return lines
