"""Render an XML/XSD document as a PDF, via a small per-schema template
mechanism.

Used by main.py's XSheet > Export to PDF menu item (see export_to_pdf()
there), but kept independent of NiceGUI/the editor so it can be tested or
reused on its own -- it just takes text in and returns PDF bytes out.

generate_pdf() parses the text and tries each registered template's
`matches(root)` in order, rendering with the first one that accepts the
document. Only one template is registered so far, for the XSheet schema's
<ExposureSheet> documents (see _render_xsheet_template): it puts the
Production fields up front, documents every other element's tag, attributes
and text as a nested outline, and appends the original source as a raw-XML
appendix. Anything that doesn't match a registered template -- a bare .xsd
schema, some other XML vocabulary, or text that doesn't even parse -- falls
back to _render_default_template, a plain paginated listing of the source
(this module's only behavior before templates existed).

To add another schema's template, write a `matches(root) -> bool` and a
`render(pdf, root, title, raw_text) -> None` and append them to _TEMPLATES.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date

from fpdf import FPDF
from fpdf.enums import XPos, YPos

PAGE_FORMAT = 'Letter'
MARGIN = 36  # points
LINE_HEIGHT = 11
BODY_FONT_SIZE = 9
INDENT_STEP = 14

XSHEET_CORE_NS = 'http://schemas.animation.org/xsheet/core'


def _sanitize(text: str) -> str:
    """The core PDF fonts (Courier/Helvetica/...) only support the Latin-1
    character range. Rather than fail on a document that happens to contain
    e.g. a curly quote or an em dash, replace anything outside that range
    with '?' -- acceptable for a report/listing, where the goal is a
    readable printout, not a lossless transcript."""
    return text.encode('latin-1', errors='replace').decode('latin-1')


def _strip_ns(tag: str) -> str:
    return tag.split('}', 1)[-1] if '}' in tag else tag


def _format_attrs(elem: ET.Element) -> str:
    return ' '.join(f'{_strip_ns(k)}="{v}"' for k, v in elem.attrib.items())


def _new_pdf() -> FPDF:
    pdf = FPDF(format=PAGE_FORMAT, unit='pt')
    pdf.set_auto_page_break(auto=True, margin=MARGIN)
    pdf.set_margins(MARGIN, MARGIN, MARGIN)
    pdf.add_page()
    return pdf


def _write_title_block(pdf: FPDF, title: str) -> None:
    pdf.set_font('Courier', 'B', 16)
    pdf.multi_cell(0, 20, _sanitize(title), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font('Courier', '', 8)
    pdf.set_text_color(110, 110, 110)
    pdf.cell(0, 12, date.today().isoformat(), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_text_color(0, 0, 0)
    pdf.ln(8)


def _write_heading(pdf: FPDF, text: str) -> None:
    pdf.set_font('Courier', 'B', 13)
    pdf.multi_cell(0, 16, _sanitize(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    y = pdf.get_y()
    pdf.set_draw_color(180, 180, 180)
    pdf.line(pdf.l_margin, y, pdf.w - pdf.r_margin, y)
    pdf.ln(6)


def _write_raw_listing(pdf: FPDF, text: str) -> None:
    """A plain, line-by-line dump of `text` in a fixed-width font, wrapping
    (rather than truncating) long lines. Used both as the default template
    for unrecognized documents and as the XSheet template's appendix."""
    pdf.set_font('Courier', '', BODY_FONT_SIZE)
    body_width = pdf.w - pdf.l_margin - pdf.r_margin
    for line in _sanitize(text).splitlines() or ['']:
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(body_width, LINE_HEIGHT, line or ' ', new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def _write_production_block(pdf: FPDF, root: ET.Element) -> None:
    """The <Production> element's own fields (ProjectID, SceneID,
    FrameRate, ...), as a key/value block at the top of the document."""
    production = next((c for c in root if _strip_ns(c.tag) == 'Production'), None)
    if production is None:
        return
    _write_heading(pdf, 'Production')
    pdf.set_font('Courier', '', BODY_FONT_SIZE)
    for field in production:
        key = _strip_ns(field.tag)
        value = ' '.join((field.text or '').split())
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(0, LINE_HEIGHT, _sanitize(f'{key}: {value}'), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(8)


def _write_element_outline(pdf: FPDF, elem: ET.Element, depth: int = 0) -> None:
    """Recursively document `elem` and its descendants: the tag name, its
    own attributes with their values, and its text for leaf elements."""
    indent = depth * INDENT_STEP
    width = pdf.w - pdf.l_margin - pdf.r_margin - indent
    tag = _strip_ns(elem.tag)
    attrs = _format_attrs(elem)
    children = list(elem)

    pdf.set_x(pdf.l_margin + indent)
    pdf.set_font('Courier', 'B', BODY_FONT_SIZE)
    pdf.multi_cell(width, LINE_HEIGHT, _sanitize(tag), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    if attrs:
        pdf.set_x(pdf.l_margin + indent + INDENT_STEP)
        pdf.set_font('Courier', '', BODY_FONT_SIZE)
        pdf.set_text_color(90, 90, 90)
        pdf.multi_cell(width - INDENT_STEP, LINE_HEIGHT, _sanitize(attrs), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_text_color(0, 0, 0)

    text = ' '.join((elem.text or '').split())
    if text and not children:
        pdf.set_x(pdf.l_margin + indent + INDENT_STEP)
        pdf.set_font('Courier', '', BODY_FONT_SIZE)
        pdf.multi_cell(width - INDENT_STEP, LINE_HEIGHT, _sanitize(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    for child in children:
        _write_element_outline(pdf, child, depth + 1)


def _is_xsheet_document(root: ET.Element) -> bool:
    return root.tag == f'{{{XSHEET_CORE_NS}}}ExposureSheet'


def _render_xsheet_template(pdf: FPDF, root: ET.Element, title: str, raw_text: str) -> None:
    """Production up front, every other top-level section documented as a
    nested tag/attribute/text outline, then the raw source as an appendix."""
    _write_title_block(pdf, title)
    _write_production_block(pdf, root)
    _write_heading(pdf, 'Document Structure')
    for child in root:
        if _strip_ns(child.tag) == 'Production':
            continue  # already shown above
        _write_element_outline(pdf, child)

    pdf.add_page()
    _write_heading(pdf, 'Appendix: Raw XML')
    _write_raw_listing(pdf, raw_text)


def _render_default_template(pdf: FPDF, root: ET.Element | None, title: str, raw_text: str) -> None:
    """Fallback for anything that isn't a recognized schema (a plain .xsd
    file, some other XML vocabulary, or text that doesn't even parse) --
    the plain listing this module always produced before templates existed."""
    _write_title_block(pdf, title)
    _write_raw_listing(pdf, raw_text)


# (matches, render) pairs, tried in order; generate_pdf() falls back to
# _render_default_template() when none match or the text isn't well-formed
# XML at all. Append further schema-specific templates here.
_TEMPLATES = [
    (_is_xsheet_document, _render_xsheet_template),
]


def generate_pdf(text: str, *, title: str = 'Untitled') -> bytes:
    """Render `text` as a PDF and return the raw PDF bytes, using whichever
    registered template's `matches()` accepts the parsed document (falling
    back to a plain source listing when none do, or the text isn't
    well-formed XML)."""
    pdf = _new_pdf()

    root = None
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        pass

    if root is not None:
        for matches, render in _TEMPLATES:
            if matches(root):
                render(pdf, root, title, text)
                return bytes(pdf.output())

    _render_default_template(pdf, root, title, text)
    return bytes(pdf.output())
