"""Render an XML/XSD document as a PDF, via a small per-schema template
mechanism.

Used by main.py's XSheet > Export Report menu item (see export_to_pdf()
there), but kept independent of NiceGUI/the editor so it can be tested or
reused on its own -- it just takes text in and returns PDF bytes out.
generate_xsheet_pdf() below is the counterpart used by XSheet > Export
XSheet, rendering the Exposure Sheet grid itself as a table rather than
the source document's element outline.

generate_pdf() parses the text and tries each registered template's
`matches(root)` in order, rendering with the first one that accepts the
document. Only one template is registered so far, for the XSheet schema's
<ExposureSheet> documents (see _render_xsheet_template): a clickable Table
of Contents, then the Production fields up front, then every other
top-level element as its own titled section documenting its tag, attributes
and text as a nested outline, then the original source as a raw-XML
appendix. The Table of Contents (via fpdf2's insert_toc_placeholder() /
start_section()) has one entry per section -- Production, each top-level
element, and the Appendix -- each a real internal PDF link that jumps to
that section's page. Anything that doesn't match a registered template -- a
bare .xsd schema, some other XML vocabulary, or text that doesn't even
parse -- falls back to _render_default_template, a plain paginated listing
of the source with no Table of Contents (this module's only behavior before
templates existed).

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


def _new_pdf(orientation: str = 'P') -> FPDF:
    pdf = FPDF(orientation=orientation, format=PAGE_FORMAT, unit='pt')
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


def _write_warning_banner(pdf: FPDF, warnings: list[str]) -> None:
    """A prominent first-page notice for when the source document didn't
    pass validation but the user chose to export it anyway (see main.py's
    export_to_pdf() / _validate_for_export())."""
    pdf.set_text_color(180, 0, 0)
    pdf.set_font('Courier', 'B', 11)
    pdf.multi_cell(0, 14, 'WARNING: this document did not pass validation:', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font('Courier', '', 9)
    for warning in warnings:
        pdf.multi_cell(0, 12, _sanitize(f'- {warning}'), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
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


def _render_toc(pdf: FPDF, outline: list) -> None:
    """Draws the Table of Contents page: one clickable, dot-leadered line
    per section start_section() recorded (Production, each top-level
    element, and the Appendix), jumping to that section's page."""
    _write_heading(pdf, 'Table of Contents')
    pdf.set_font('Courier', '', BODY_FONT_SIZE + 1)
    for section in outline:
        link = pdf.add_link(page=section.page_number)
        name = _sanitize(section.name)
        leader_len = max(3, 64 - len(name))
        label = f'{name} {"." * leader_len} {section.page_number}'
        pdf.set_x(pdf.l_margin)
        pdf.set_text_color(20, 60, 140)
        pdf.multi_cell(0, 18, label, new_x=XPos.LMARGIN, new_y=YPos.NEXT, link=link)
        pdf.set_text_color(0, 0, 0)


def _write_production_block(pdf: FPDF, root: ET.Element) -> None:
    """The <Production> element's own fields (ProjectID, SceneID,
    FrameRate, ...), as a key/value block at the top of the document."""
    production = next((c for c in root if _strip_ns(c.tag) == 'Production'), None)
    if production is None:
        return
    pdf.start_section('Production', level=0)
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


def _write_section(pdf: FPDF, elem: ET.Element) -> None:
    """Document one top-level element as its own titled section: a heading
    for its tag name, its own attributes/text (if any), then each of its
    children as a nested outline. Blank space before the heading and
    between each direct child keeps sections and their entries -- each
    Asset, each Frame, each Review, etc. -- visually distinct blocks rather
    than a wall of text."""
    pdf.ln(14)
    tag = _strip_ns(elem.tag)
    pdf.start_section(tag, level=0)
    _write_heading(pdf, tag)

    attrs = _format_attrs(elem)
    if attrs:
        pdf.set_x(pdf.l_margin)
        pdf.set_font('Courier', '', BODY_FONT_SIZE)
        pdf.set_text_color(90, 90, 90)
        pdf.multi_cell(pdf.w - pdf.l_margin - pdf.r_margin, LINE_HEIGHT, _sanitize(attrs), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_text_color(0, 0, 0)
        pdf.ln(4)

    children = list(elem)
    text = ' '.join((elem.text or '').split())
    if text and not children:
        pdf.set_x(pdf.l_margin)
        pdf.set_font('Courier', '', BODY_FONT_SIZE)
        pdf.multi_cell(0, LINE_HEIGHT, _sanitize(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    for child in children:
        _write_element_outline(pdf, child)
        pdf.ln(6)


def _is_xsheet_document(root: ET.Element) -> bool:
    return root.tag == f'{{{XSHEET_CORE_NS}}}ExposureSheet'


def _render_xsheet_template(pdf: FPDF, root: ET.Element, title: str, raw_text: str) -> None:
    """A clickable Table of Contents, Production up front, every other
    top-level element as its own titled, spaced-out section, then the raw
    source as an appendix."""
    _write_title_block(pdf, title)

    pdf.add_page()
    pdf.insert_toc_placeholder(_render_toc, pages=1)

    _write_production_block(pdf, root)
    for child in root:
        if _strip_ns(child.tag) == 'Production':
            continue  # already shown above
        _write_section(pdf, child)

    pdf.add_page()
    pdf.start_section('Appendix: Raw XML', level=0)
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


def generate_pdf(text: str, *, title: str = 'Untitled', warnings: list[str] | None = None) -> bytes:
    """Render `text` as a PDF and return the raw PDF bytes, using whichever
    registered template's `matches()` accepts the parsed document (falling
    back to a plain source listing when none do, or the text isn't
    well-formed XML). When `warnings` is non-empty, a prominent notice is
    stamped onto the first page, above the title, before that template's
    own content -- see main.py's export_to_pdf(), which populates this from
    the document's own well-formedness/schema-validation problems."""
    pdf = _new_pdf()
    if warnings:
        _write_warning_banner(pdf, warnings)

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


def generate_xsheet_pdf(layer_ids: list[str], rows: list[dict], *, title: str = 'Untitled') -> bytes:
    """Render the Exposure Sheet grid (as already computed by main.py's
    parse_exposure_sheet()) as a paginated, landscape table PDF: one row per
    frame number, one column per layer plus Camera/Dialogue/Audio/Notes --
    the same shape as the XSheet tab's on-screen grid, including its blank
    hold rows between sparse <Frame> entries. Used by main.py's XSheet >
    Export XSheet menu item (see export_xsheet() there)."""
    pdf = _new_pdf(orientation='L')
    _write_title_block(pdf, f'{title} - Exposure Sheet')

    headers = ['Frame', *layer_ids, 'Camera', 'Dialogue', 'Audio', 'Notes']
    centered = {'Frame', 'Camera', 'Audio', *layer_ids}
    col_widths = [0.6, *([0.9] * len(layer_ids)), 1.2, 1.3, 0.9, 1.6]

    pdf.set_font('Courier', '', 8)
    with pdf.table(col_widths=tuple(col_widths), text_align='LEFT', line_height=11) as table:
        header_row = table.row()
        for header in headers:
            header_row.cell(_sanitize(header), align='C')
        for row in rows:
            data_row = table.row()
            for header in headers:
                value = row.get(header, '')
                data_row.cell(_sanitize(str(value)), align='C' if header in centered else 'L')

    return bytes(pdf.output())
