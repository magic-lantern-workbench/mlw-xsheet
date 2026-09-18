"""Render an XML/XSD document's source text as a paginated PDF listing.

Used by main.py's XSheet > Export to PDF menu item (see export_to_pdf()
there), but kept independent of NiceGUI/the editor so it can be tested or
reused on its own -- it just takes text in and returns PDF bytes out.
"""

from __future__ import annotations

from datetime import date

from fpdf import FPDF
from fpdf.enums import XPos, YPos

PAGE_FORMAT = 'Letter'
MARGIN = 36  # points
LINE_HEIGHT = 11
BODY_FONT_SIZE = 8


def _sanitize(text: str) -> str:
    """The core PDF fonts (Courier/Helvetica/...) only support the Latin-1
    character range. Rather than fail on a document that happens to contain
    e.g. a curly quote or an em dash, replace anything outside that range
    with '?' -- acceptable for a source listing, where the goal is a
    readable printout, not a lossless transcript."""
    return text.encode('latin-1', errors='replace').decode('latin-1')


def generate_pdf(text: str, *, title: str = 'Untitled') -> bytes:
    """Render `text` (an XML/XSD document's source) as a paginated PDF and
    return the raw PDF bytes, in a fixed-width font, wrapping (rather than
    truncating) long lines."""
    pdf = FPDF(format=PAGE_FORMAT, unit='pt')
    pdf.set_auto_page_break(auto=True, margin=MARGIN)
    pdf.set_margins(MARGIN, MARGIN, MARGIN)
    pdf.add_page()

    pdf.set_font('Courier', 'B', 13)
    pdf.cell(0, 18, _sanitize(title), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font('Courier', '', 8)
    pdf.set_text_color(110, 110, 110)
    pdf.cell(0, 12, date.today().isoformat(), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_text_color(0, 0, 0)
    pdf.ln(6)

    pdf.set_font('Courier', '', BODY_FONT_SIZE)
    body_width = pdf.w - pdf.l_margin - pdf.r_margin

    lines = _sanitize(text).splitlines() or ['']
    for line in lines:
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(body_width, LINE_HEIGHT, line or ' ', new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    return bytes(pdf.output())
