# COPYRIGHT_BEGIN
#
# MIT License
#
# Copyright (c) 2026 Wizzer Works
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
# COPYRIGHT_END

"""Render an XML/XSD document as a PDF, via a small per-schema template
mechanism.

Used by main.py's XSheet > Generate Report menu item (see export_to_pdf()
there), but kept independent of NiceGUI/the editor so it can be tested or
reused on its own -- it just takes text in and returns PDF bytes out.
generate_xsheet_pdf() below is the counterpart used by XSheet > Export
XSheet, rendering the Exposure Sheet grid itself as a table rather than
the source document's element outline.

generate_pdf() parses the text and tries each registered template's
`matches(root)` in order, rendering with the first one that accepts the
document. Only one template is registered so far, for the XSheet schema's
<ExposureSheet> documents (see _render_xsheet_template): the Production
fields on page 1, a clickable Table of Contents starting on page 2, then
every other top-level element as its own titled section documenting its
tag, attributes and text as a nested outline, then the original source as
a raw-XML appendix. The Table of Contents (via fpdf2's insert_toc_placeholder() /
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
from datetime import date, datetime, timezone

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
    timestamp_utc = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    pdf.cell(0, 12, f'Created: {timestamp_utc}', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
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
    """Production information, then VersionControl, both on page 1 (right
    under the title), a clickable Table of Contents starting on page 2,
    every other top-level element as its own titled, spaced-out section,
    then the raw source as an appendix."""
    _write_title_block(pdf, title)
    _write_production_block(pdf, root)

    version_control = next((c for c in root if _strip_ns(c.tag) == 'VersionControl'), None)
    if version_control is not None:
        _write_section(pdf, version_control)

    pdf.add_page()
    pdf.insert_toc_placeholder(_render_toc, pages=1)

    for child in root:
        if _strip_ns(child.tag) in ('Production', 'VersionControl'):
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


# Production fields shown above the grid, as in the XSheet view.
XSHEET_PRODUCTION_FIELDS = (('ProjectID', 'Project ID'), ('SequenceID', 'Sequence ID'), ('SceneID', 'Scene ID'),
                            ('Title', 'Title'), ('FrameRate', 'Frame Rate'))


def _write_grid_header(pdf: FPDF, root: ET.Element | None, layer_ids: list[str], rows: list[dict]) -> None:
    """The same header the XSheet view shows above its grid: "Exposure
    Sheet" with the frame/layer counts, then the Production info line."""
    pdf.set_font('Helvetica', 'B', 11)
    pdf.cell(pdf.get_string_width('Exposure Sheet') + 8, 16, 'Exposure Sheet')
    pdf.set_font('Helvetica', '', 9)
    pdf.set_text_color(110, 110, 110)
    pdf.cell(0, 16, _sanitize(f'{len(rows)} frame(s), {len(layer_ids)} layer(s).'), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    production = next((c for c in root if _strip_ns(c.tag) == 'Production'), None) if root is not None else None
    if production is not None:
        values = {_strip_ns(f.tag): ' '.join((f.text or '').split()) for f in production}
        for key, caption in XSHEET_PRODUCTION_FIELDS:
            value = values.get(key) or '-'
            if key == 'FrameRate' and value != '-':
                value = f'{value} fps'
            pdf.set_font('Helvetica', '', 9)
            pdf.set_text_color(110, 110, 110)
            pdf.cell(pdf.get_string_width(f'{caption}: ') + 1, 14, _sanitize(f'{caption}: '))
            pdf.set_font('Helvetica', 'B', 9)
            pdf.set_text_color(0, 0, 0)
            pdf.cell(pdf.get_string_width(_sanitize(value)) + 18, 14, _sanitize(value))
        pdf.ln(14)
    pdf.set_text_color(0, 0, 0)
    pdf.ln(6)


def _draw_traditional_table(pdf: FPDF, columns: list[dict], rows: list[dict]) -> None:
    """Draw the traditional exposure-sheet table by hand, like the XSheet
    view: centred bold headings (repeated on each page), rows that grow to
    fit wrapped text, alternating shading, grey frame-number columns, and a
    heavier rule after the last frame of each second (rows with _second).
    Each column: key, header, weight (relative width), align, wrap."""
    font_size, line_h, pad = 7.5, 9.0, 2.5
    usable = pdf.w - pdf.l_margin - pdf.r_margin
    total = sum(c['weight'] for c in columns)
    widths = [usable * c['weight'] / total for c in columns]
    bottom = pdf.h - pdf.b_margin
    grid, rule, shade, frame_fill = (208, 208, 208), (85, 85, 85), (247, 247, 247), (241, 241, 241)

    def text_lines(text: str, width: float) -> list[str]:
        if not text:
            return ['']
        return pdf.multi_cell(width - 2 * pad, line_h, _sanitize(text), dry_run=True, output='LINES')

    def draw_header():
        pdf.set_font('Helvetica', 'B', font_size)
        y, x = pdf.get_y(), pdf.l_margin
        height = line_h + 2 * pad
        for col, width in zip(columns, widths):
            pdf.set_fill_color(240, 240, 240)
            pdf.set_draw_color(*grid)
            pdf.rect(x, y, width, height, style='DF')
            pdf.set_xy(x, y + pad)
            pdf.cell(width, line_h, _sanitize(col['header']), align='C')
            x += width
        pdf.set_y(y + height)
        pdf.set_font('Helvetica', '', font_size)

    # The heavier second rules sit on the edge shared with the next row, so
    # they're drawn once a page is complete -- otherwise the next row's
    # filled background paints over them.
    rules: list[float] = []

    def draw_rules():
        pdf.set_draw_color(*rule)
        pdf.set_line_width(1.4)
        for y in rules:
            pdf.line(pdf.l_margin, y, pdf.l_margin + usable, y)
        pdf.set_line_width(0.4)
        rules.clear()

    pdf.set_auto_page_break(False)
    draw_header()
    for index, row in enumerate(rows):
        cells = []
        for col, width in zip(columns, widths):
            value = str(row.get(col['key'], '') or '')
            lines = text_lines(value, width) if col.get('wrap') else [value]
            cells.append(lines)
        height = max(len(lines) for lines in cells) * line_h + 2 * pad
        if pdf.get_y() + height > bottom:
            draw_rules()
            pdf.add_page()
            draw_header()
        y, x = pdf.get_y(), pdf.l_margin
        for col, width, lines in zip(columns, widths, cells):
            fill = frame_fill if col.get('frame') else (shade if index % 2 else (255, 255, 255))
            pdf.set_fill_color(*fill)
            pdf.set_draw_color(*grid)
            pdf.set_line_width(0.4)
            pdf.rect(x, y, width, height, style='DF')
            for n, line in enumerate(lines):
                pdf.set_xy(x + pad, y + pad + n * line_h)
                pdf.cell(width - 2 * pad, line_h, _sanitize(line), align='C' if col.get('align') == 'C' else 'L')
            x += width
        if row.get('_second'):
            rules.append(y + height)
        pdf.set_y(y + height)
    draw_rules()
    pdf.set_draw_color(0, 0, 0)
    pdf.set_auto_page_break(True, margin=MARGIN)


def generate_xsheet_pdf(layer_ids: list[str], rows: list[dict], *,
                        title: str = 'Untitled', source_text: str | None = None,
                        style: str = 'classic', headings: dict[str, str] | None = None) -> bytes:
    """Render the Exposure Sheet grid (as already computed by main.py's
    parse_exposure_sheet()) as a paginated, landscape PDF, in either XSheet
    style -- the same shape as the XSheet tab's on-screen grid, including
    the blank hold rows between sparse <Frame> entries (every frame is
    printed; runs aren't collapsed). Used by main.py's XSheet > Export
    XSheet menu item (see export_xsheet() there).

    style: 'classic' (Frame, one column per layer, Camera, Dialogue, Audio,
    Notes) or 'traditional' (Action/Description, Fr, Audio, Dialogue,
    Sound FX, Tech. Notes, the layers, Fr, Camera Moves, with alternating
    shading and a heavier rule after each second). headings: the user's own
    names for the traditional Sound FX / Tech. Notes columns ('soundfx',
    'technotes').

    `source_text` (the same raw document export_xsheet() parsed to build
    layer_ids/rows) is used, best-effort, to show the document's
    <Production> and <VersionControl> fields on page 1, and the view's
    header (counts and Production info) above the grid, which always starts
    on page 2."""
    pdf = _new_pdf(orientation='L')
    _write_title_block(pdf, f'{title} - Exposure Sheet')

    root = None
    if source_text:
        try:
            root = ET.fromstring(source_text)
        except ET.ParseError:
            root = None
        if root is not None:
            _write_production_block(pdf, root)
            version_control = next((c for c in root if _strip_ns(c.tag) == 'VersionControl'), None)
            if version_control is not None:
                _write_section(pdf, version_control)

    pdf.add_page()
    _write_grid_header(pdf, root, layer_ids, rows)

    if style == 'traditional':
        headings = headings or {}
        columns = [
            {'key': 'Notes', 'header': 'Action/Description', 'weight': 2.4, 'wrap': True},
            {'key': 'Frame', 'header': 'Fr', 'weight': 0.45, 'align': 'C', 'frame': True},
            {'key': 'AudioTrack', 'header': 'Audio', 'weight': 1.0, 'align': 'C'},
            {'key': 'Dialogue', 'header': 'Dialogue', 'weight': 1.3, 'wrap': True},
            {'key': 'SoundFX', 'header': headings.get('soundfx', 'Sound FX'), 'weight': 0.9, 'align': 'C'},
            {'key': 'TechNotes', 'header': headings.get('technotes', 'Tech. Notes'), 'weight': 1.5, 'wrap': True},
            *({'key': lid, 'header': lid, 'weight': 0.8, 'align': 'C'} for lid in layer_ids),
            {'key': 'Frame', 'header': 'Fr', 'weight': 0.45, 'align': 'C', 'frame': True},
            {'key': 'Camera', 'header': 'Camera Moves', 'weight': 1.1, 'align': 'C'},
        ]
        _draw_traditional_table(pdf, columns, rows)
        return bytes(pdf.output())

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
