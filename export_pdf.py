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


# The top-level ExposureSheet sections a report can include or leave out
# (see generate_pdf()'s `sections`). Production and VersionControl are
# always included.
REPORT_SECTIONS = ('Assets', 'AudioTracks', 'Camera', 'Timeline', 'Reviews', 'OTIO')
ALWAYS_REPORTED = ('Production', 'VersionControl')


def _render_xsheet_template(pdf: FPDF, root: ET.Element, title: str, raw_text: str, *,
                            sections: set[str] | None = None, include_raw: bool = True) -> None:
    """Production information, then VersionControl, both on page 1 (right
    under the title), a clickable Table of Contents starting on page 2,
    every other top-level element as its own titled, spaced-out section,
    then the raw source as an appendix.

    sections: which of the optional top-level elements (REPORT_SECTIONS) to
    include (None: all); Production and VersionControl are always included.
    include_raw: whether to add the appendix, which lists the whole
    document."""
    def included(elem):
        tag = _strip_ns(elem.tag)
        return sections is None or tag in ALWAYS_REPORTED or tag in sections

    _write_title_block(pdf, title)
    _write_production_block(pdf, root)

    version_control = next((c for c in root if _strip_ns(c.tag) == 'VersionControl'), None)
    if version_control is not None and included(version_control):
        _write_section(pdf, version_control)

    body = [c for c in root if _strip_ns(c.tag) not in ('Production', 'VersionControl') and included(c)]
    if body or include_raw:
        pdf.add_page()
        pdf.insert_toc_placeholder(_render_toc, pages=1)

    for child in body:
        _write_section(pdf, child)

    if include_raw:
        pdf.add_page()
        pdf.start_section('Appendix: Raw XML', level=0)
        _write_heading(pdf, 'Appendix: Raw XML')
        _write_raw_listing(pdf, raw_text)


def _render_default_template(pdf: FPDF, root: ET.Element | None, title: str, raw_text: str, **_options) -> None:
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


def generate_pdf(text: str, *, title: str = 'Untitled', warnings: list[str] | None = None,
                 sections: set[str] | None = None, include_raw: bool = True) -> bytes:
    """Render `text` as a PDF and return the raw PDF bytes, using whichever
    registered template's `matches()` accepts the parsed document (falling
    back to a plain source listing when none do, or the text isn't
    well-formed XML). When `warnings` is non-empty, a prominent notice is
    stamped onto the first page, above the title, before that template's
    own content -- see main.py's export_to_pdf(), which populates this from
    the document's own well-formedness/schema-validation problems.

    sections / include_raw: for ExposureSheet documents, which optional
    top-level elements to include (None: all; see REPORT_SECTIONS --
    Production and VersionControl are always included) and whether to add
    the raw XML appendix of the whole document. Other documents ignore them."""
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
                render(pdf, root, title, text, sections=sections, include_raw=include_raw)
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


# How _draw_sheet_table() draws each XSheet style. traditional: Helvetica,
# padded rows, a grey heading bar, alternating shading and grey frame-number
# columns, like the view; classic: plain Courier rows with thin grey lines.
_SHEET_LOOKS = {
    'traditional': {'font': 'Helvetica', 'font_size': 7.5, 'line_h': 9.0, 'pad_x': 2.5, 'pad_y': 2.5,
                    'grid': (208, 208, 208), 'grid_w': 0.4, 'header_fill': (240, 240, 240),
                    'shade': (247, 247, 247), 'frame_fill': (241, 241, 241)},
    'classic': {'font': 'Courier', 'font_size': 8, 'line_h': 11.0, 'pad_x': 1.0, 'pad_y': 0.0,
                'grid': (160, 160, 160), 'grid_w': 0.4, 'header_fill': None,
                'shade': None, 'frame_fill': None},
}


def _draw_sheet_table(pdf: FPDF, columns: list[dict], rows: list[dict], style: str = 'traditional',
                      screen: dict | None = None) -> dict:
    """Draw an XSheet table by hand, in the look of `style` (see
    _SHEET_LOOKS): centred bold headings (repeated on each page), rows that
    grow to fit wrapped text, and -- in both styles, like the view -- a
    heavier rule after the last frame of each second (rows with _second).
    Each column: key, header, weight (relative width), align, wrap, frame.

    `screen` (see _screen_layout()), to draw a sketch over it: the table laid
    out as the XSheet tab shows it, at one scale -- its columns' 'lefts' and
    'widths' and each frame's row height ('heights'); its rows don't grow,
    so text that doesn't fit is cut short, as on screen, and the type is made
    smaller if the rows are too short for it.

    Returns where it all went, for _draw_sketch(): the columns' 'lefts' and
    'widths', and 'rows' -- each row's (page, top, height, frame)."""
    look = _SHEET_LOOKS[style]
    font, font_size, line_h = look['font'], look['font_size'], look['line_h']
    pad_x, pad_y = look['pad_x'], look['pad_y']
    usable = pdf.w - pdf.l_margin - pdf.r_margin
    if screen:
        widths, lefts = list(screen['widths']), list(screen['lefts'])
        fit = min(1.0, min(screen['heights'].values(), default=screen['row_h']) / (line_h + 2 * pad_y))
        font_size, line_h, pad_x, pad_y = font_size * fit, line_h * fit, pad_x * fit, pad_y * fit
    else:
        total = sum(c['weight'] for c in columns)
        widths = [usable * c['weight'] / total for c in columns]
        lefts = [pdf.l_margin + sum(widths[:i]) for i in range(len(widths))]
    table_right = lefts[-1] + widths[-1]
    bottom = pdf.h - pdf.b_margin
    grid, rule = look['grid'], (85, 85, 85)
    white = (255, 255, 255)
    shade = look['shade'] or white
    frame_fill = look['frame_fill']

    def text_lines(text: str, width: float) -> list[str]:
        if not text:
            return ['']
        return pdf.multi_cell(width - 2 * pad_x, line_h, _sanitize(text), dry_run=True, output='LINES')

    def cut(text: str, width: float) -> str:  # what fits in a width, as the screen shows it
        text = _sanitize(text)
        if pdf.get_string_width(text) <= width:
            return text
        while text and pdf.get_string_width(text + '...') > width:
            text = text[:-1]
        return text + '...' if text else ''

    def draw_header():
        pdf.set_font(font, 'B', font_size)
        y = pdf.get_y()
        height = line_h + 2 * pad_y
        for col, x, width in zip(columns, lefts, widths):
            pdf.set_fill_color(*(look['header_fill'] or white))
            pdf.set_draw_color(*grid)
            pdf.set_line_width(look['grid_w'])
            pdf.rect(x, y, width, height, style='DF')
            pdf.set_xy(x, y + pad_y)
            header = cut(col['header'], width - 2 * pad_x) if screen else _sanitize(col['header'])
            pdf.cell(width, line_h, header, align='C')
        pdf.set_y(y + height)
        pdf.set_font(font, '', font_size)

    # The heavier second rules sit on the edge shared with the next row, so
    # they're drawn once a page is complete -- otherwise the next row's
    # filled background paints over them.
    rules: list[float] = []

    def draw_rules():
        pdf.set_draw_color(*rule)
        pdf.set_line_width(1.4)
        for y in rules:
            pdf.line(lefts[0], y, table_right, y)
        pdf.set_line_width(look['grid_w'])
        rules.clear()

    placed: list[tuple[int, float, float, object]] = []
    pdf.set_auto_page_break(False)
    draw_header()
    for index, row in enumerate(rows):
        cells = []
        if screen:
            height = screen['heights'].get(row.get('Frame'), screen['row_h'])
            fits = max(int((height - 2 * pad_y) / line_h + 1e-6), 1)  # lines a row has room for
        for col, width in zip(columns, widths):
            value = str(row.get(col['key'], '') or '')
            if screen:
                lines = text_lines(value, width)[:fits] if col.get('wrap') and fits > 1 else [value]
                lines = [cut(line, width - 2 * pad_x) for line in lines]
            else:
                lines = text_lines(value, width) if col.get('wrap') else [value]
            cells.append(lines)
        if not screen:
            height = max(len(lines) for lines in cells) * line_h + 2 * pad_y
        if pdf.get_y() + height > bottom:
            draw_rules()
            pdf.add_page()
            draw_header()
        y = pdf.get_y()
        for col, x, width, lines in zip(columns, lefts, widths, cells):
            fill = frame_fill if col.get('frame') and frame_fill else (shade if index % 2 else white)
            pdf.set_fill_color(*fill)
            pdf.set_draw_color(*grid)
            pdf.set_line_width(look['grid_w'])
            pdf.rect(x, y, width, height, style='DF')
            # on screen a line sits in the middle of its row
            text_top = y + (height - len(lines) * line_h) / 2 if screen else y + pad_y
            for n, line in enumerate(lines):
                pdf.set_xy(x + pad_x, text_top + n * line_h)
                pdf.cell(width - 2 * pad_x, line_h, _sanitize(line), align='C' if col.get('align') == 'C' else 'L')
        if row.get('_second'):
            rules.append(y + height)
        placed.append((pdf.page, y, height, row.get('Frame')))
        pdf.set_y(y + height)
    draw_rules()
    pdf.set_draw_color(0, 0, 0)
    pdf.set_line_width(0.567)  # fpdf2's default (0.2 mm)
    pdf.set_auto_page_break(True, margin=MARGIN)
    return {'lefts': lefts, 'widths': widths, 'rows': placed}


def _screen_layout(pdf: FPDF, columns: list[dict], sketch: dict | None) -> dict | None:
    """The table laid out as the XSheet tab shows it, for a sketch to be
    drawn over it undistorted (see _draw_sheet_table() and _draw_sketch()):
    the grid's columns and row heights from the browser (see
    mlwSketchpad.sheetShapes()), all at one scale -- the page's width over
    the columns, or as far to the right as the sketch goes if that's
    further (up to the grid's own width), so a sketch in the space beside
    the columns is kept too. None without a sketch, or if the browser's
    columns aren't these."""
    if not sketch or not sketch.get('shapes') or len(sketch.get('columns') or []) != len(columns):
        return None
    cols = sketch['columns']
    right = max(l + w for l, w in cols)
    reach = max((x + shape.get('width', 0) / 2 for shape in sketch['shapes'] for x, _ in shape.get('points') or []),
                default=0)
    extent = max(right, min(reach, sketch.get('width') or reach))
    scale = (pdf.w - pdf.l_margin - pdf.r_margin) / extent
    heights: dict[int, float] = {}
    standard = min(h for _, _, h in sketch['rows'])
    for first, last, h in sketch['rows']:
        for frame in range(int(first), int(last) + 1):  # a collapsed run's frames: a row each, as printed
            heights[frame] = (h if first == last else standard) * scale
    return {'scale': scale, 'row_h': standard * scale, 'heights': heights,
            'lefts': [pdf.l_margin + l * scale for l, _ in cols], 'widths': [w * scale for _, w in cols]}


def _draw_sketch(pdf: FPDF, layout: dict, sketch: dict | None, screen: dict | None) -> None:
    """Draw the XSheet tab's Sketchpad sketch over the table
    _draw_sheet_table() drew (its `layout`), on each page it crosses and
    clipped to that page's rows. The sketch is as the browser gives it (see
    mlwSketchpad.sheetShapes()): each shape's colour, width, whether it's
    closed, and its points as [x, frame] -- pixels from the grid's left, and
    a frame's number plus how far down it. With the table laid out as on
    screen (`screen`, see _screen_layout()), a point's place is its pixels at
    that one scale, so the sketch isn't distorted, and a mark in the space
    beside the columns is kept; a mark made on a frame lands on its row,
    whichever page that's on, and a mark crossing a page break carries on
    from one page to the next."""
    from bisect import bisect_right
    from fpdf.enums import StrokeCapStyle, StrokeJoinStyle
    placed = [(page, top, h, frame) for page, top, h, frame in layout['rows'] if isinstance(frame, int)]
    if not placed or not sketch or not sketch.get('shapes'):
        return
    lefts, widths = layout['lefts'], layout['widths']
    if screen:
        scale, clip_w = screen['scale'], pdf.w - pdf.l_margin - pdf.r_margin
    else:  # not laid out as on screen: stretched over the table as it is
        right = max((l + w for l, w in sketch.get('columns') or []), default=1)
        scale, clip_w = (lefts[-1] + widths[-1] - pdf.l_margin) / right, lefts[-1] + widths[-1] - pdf.l_margin
    frames = [frame for *_, frame in placed]
    # every row one after the other, as if on one long page
    starts, run = [], 0.0
    for _, _, h, _ in placed:
        starts.append(run)
        run += h
    total = run

    def down(f: float) -> float:  # a frame position -> how far down the long page
        if f < frames[0]:
            return (f - frames[0]) * placed[0][2]
        if f >= frames[-1] + 1:
            return total + (f - frames[-1] - 1) * placed[-1][2]
        k = bisect_right(frames, f) - 1
        after = frames[k + 1] if k + 1 < len(frames) else frames[k] + 1
        return starts[k] + (f - frames[k]) / max(after - frames[k], 1) * placed[k][2]

    # each page's stretch of the long page, and where it's drawn
    pages: dict[int, list[float]] = {}
    for (page, top, h, _), start in zip(placed, starts):
        if page not in pages:
            pages[page] = [start, start + h, top]
        pages[page][1] = start + h
    shapes = []
    for shape in sketch['shapes']:
        points = [(pdf.l_margin + x * scale, down(f)) for x, f in shape.get('points') or []]
        if not points:
            continue
        colour = str(shape.get('color') or '#000000').lstrip('#')
        try:
            rgb = tuple(int(colour[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            rgb = (0, 0, 0)
        width = max(float(shape.get('width') or 0) * scale, 0.2)
        shapes.append((points, rgb, width, bool(shape.get('closed'))))
    last_page = pdf.page
    for page, (v0, v1, top) in pages.items():
        pdf.page = page
        with pdf.rect_clip(pdf.l_margin, top, clip_w, v1 - v0):
            for points, rgb, width, closed in shapes:
                ys = [v for _, v in points]
                if max(ys) < v0 - width or min(ys) > v1 + width:
                    continue  # not on this page
                with pdf.local_context(draw_color=rgb, line_width=width, stroke_cap_style=StrokeCapStyle.ROUND,
                                       stroke_join_style=StrokeJoinStyle.ROUND):
                    pdf.polyline([(x, top + v - v0) for x, v in points], polygon=closed, style='D')
    pdf.page = last_page


def generate_xsheet_pdf(layer_ids: list[str], rows: list[dict], *,
                        title: str = 'Untitled', source_text: str | None = None,
                        style: str = 'classic', sketch: dict | None = None) -> bytes:
    """Render the Exposure Sheet grid (as already computed by main.py's
    parse_exposure_sheet()) as a paginated, landscape PDF, in either XSheet
    style -- the same shape as the XSheet tab's on-screen grid, including
    the blank hold rows between sparse <Frame> entries (every frame is
    printed; runs aren't collapsed). Used by main.py's XSheet > Export
    XSheet menu item (see export_xsheet() there).

    style: 'classic' (Frame, one column per layer, Camera, Dialogue, Audio,
    Notes) or 'traditional' (Action/Description, Fr, Audio, Dialogue,
    Sound FX, Tech. Notes, the layers, Fr, Camera Moves, with alternating
    shading). Both have a heavier rule after each second.

    `source_text` (the same raw document export_xsheet() parsed to build
    layer_ids/rows) is used, best-effort, to show the document's
    <Production> and <VersionControl> fields on page 1, and the view's
    header (counts and Production info) above the grid, which always starts
    on page 2.

    `sketch`: the XSheet tab's Sketchpad sketch and the grid's layout on
    screen, from the browser (see mlwSketchpad.sheetShapes()); with one, the
    grid is laid out as on screen, at one scale, and the sketch drawn over it
    undistorted (see _screen_layout() and _draw_sketch()). None for none."""
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
        columns = [
            {'key': 'Notes', 'header': 'Action/Description', 'weight': 2.4, 'wrap': True},
            {'key': 'Frame', 'header': 'Fr', 'weight': 0.45, 'align': 'C', 'frame': True},
            {'key': 'AudioTrack', 'header': 'Audio', 'weight': 1.0, 'align': 'C'},
            {'key': 'Dialogue', 'header': 'Dialogue', 'weight': 1.3, 'wrap': True},
            {'key': 'SoundFX', 'header': 'Sound FX', 'weight': 0.9, 'align': 'C'},
            {'key': 'TechNotes', 'header': 'Tech. Notes', 'weight': 1.5, 'wrap': True},
            *({'key': lid, 'header': lid, 'weight': 0.8, 'align': 'C'} for lid in layer_ids),
            {'key': 'Frame', 'header': 'Fr', 'weight': 0.45, 'align': 'C', 'frame': True},
            {'key': 'Camera', 'header': 'Camera Moves', 'weight': 1.1, 'align': 'C'},
        ]
        screen = _screen_layout(pdf, columns, sketch)
        layout = _draw_sheet_table(pdf, columns, rows, 'traditional', screen)
        _draw_sketch(pdf, layout, sketch, screen)
        return bytes(pdf.output())

    columns = [
        {'key': 'Frame', 'header': 'Frame', 'weight': 0.6, 'align': 'C', 'wrap': True},
        *({'key': lid, 'header': lid, 'weight': 0.9, 'align': 'C', 'wrap': True} for lid in layer_ids),
        {'key': 'Camera', 'header': 'Camera', 'weight': 1.2, 'align': 'C', 'wrap': True},
        {'key': 'Dialogue', 'header': 'Dialogue', 'weight': 1.3, 'wrap': True},
        {'key': 'Audio', 'header': 'Audio', 'weight': 0.9, 'align': 'C', 'wrap': True},
        {'key': 'Notes', 'header': 'Notes', 'weight': 1.6, 'wrap': True},
    ]
    screen = _screen_layout(pdf, columns, sketch)
    layout = _draw_sheet_table(pdf, columns, rows, 'classic', screen)
    _draw_sketch(pdf, layout, sketch, screen)
    return bytes(pdf.output())
