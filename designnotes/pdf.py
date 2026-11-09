"""PDF export of engineering notes, rendered from the same structure as the Markdown notes."""
import io
from pathlib import Path

from fpdf import FPDF

from designnotes.extraction import SECTIONS
from designnotes.report import CONTEXT_HEADINGS, NOT_DISCUSSED, item_detail_lines, item_title, references, render_markdown
from designnotes.transcript import source_label
from designnotes.visuals import annotate, caption

REPLACEMENTS = {'\u2018': "'", '\u2019': "'", '\u201c': '"', '\u201d': '"', '\u2013': '-', '\u2014': '-',
                '\u2212': '-', '\u2026': '...', '\u00b7': '-', '\u2022': '-', '\u2264': '<=', '\u2265': '>=',
                '\u2248': '~', '\u2260': '!=', '\u03a9': 'Ohm', '\u2126': 'Ohm', '\u03bc': 'u', '\u0394': 'Delta',
                '\u03b8': 'theta', '\u03c3': 'sigma', '\u03c4': 'tau', '\u2192': '->', '\u00a0': ' '}


def latin(text):
    text = ''.join(REPLACEMENTS.get(ch, ch) for ch in str(text))
    return text.encode('latin-1', 'replace').decode('latin-1')


class NotesPDF(FPDF):
    def footer(self):
        self.set_y(-12)
        self.set_font('Helvetica', '', 8)
        self.set_text_color(110)
        self.cell(0, 8, f'Page {self.page_no()}', align='C')


def render_pdf(record, visual_dir=None):
    notes, transcript = record['notes'], record['transcript']
    segments_by_id = {s['id']: s for s in transcript['segments']}
    sections = notes['sections']
    pdf = NotesPDF(format='A4')
    pdf.set_auto_page_break(True, margin=16)
    pdf.set_margins(18, 16, 18)
    pdf.add_page()
    width = pdf.w - pdf.l_margin - pdf.r_margin

    def para(text, size=10, style='', color=30, indent=0, gap=1.5):
        pdf.set_font('Helvetica', style, size)
        pdf.set_text_color(color)
        pdf.set_x(pdf.l_margin + indent)
        pdf.multi_cell(width - indent, size * 0.5, latin(text), new_x='LMARGIN', new_y='NEXT')
        pdf.ln(gap)

    # Header block reuses the Markdown metadata lines.
    header = render_markdown(record).split('\n## ')[0].split('\n')
    para(notes['title'], 17, 'B', 20, gap=2)
    for line in header[2:]:
        if line.startswith('**'):
            para(line.replace('**', '').rstrip(), 9, '', 70, gap=0.5)
    pdf.ln(2)
    para('Source tags such as [S014 - 00:03:15 - Alice] point to transcript segments in the appendix. '
         '"Interpretation" marks model readings; "needs verification" marks items that failed an automatic check.',
         8, 'I', 90, gap=3)

    def item(section, entry):
        title, suffix = item_title(section, entry)
        suffix = suffix.replace('_', '')
        para('- ' + title + suffix, 10, 'B', 25, gap=0.3)
        refs = references(entry, segments_by_id)
        if refs:
            para(refs, 8, '', 40, indent=4, gap=0.3)
        for label, value in item_detail_lines(section, entry):
            para(f'{label}: {value}', 9, 'I' if label in ('Source quote', 'Verification') else '',
                 150 if label == 'Verification' else 60, indent=4, gap=0.3)
        for visual in entry.get('visuals') or []:
            path = Path(visual_dir or '.') / visual['file']
            if not visual_dir or not path.is_file():
                continue
            from PIL import Image
            with Image.open(path) as picture:
                aspect = picture.height / picture.width
            image_width = min(120, width - 4, 150 / aspect)
            height = image_width * aspect
            if pdf.get_y() + height > pdf.h - pdf.b_margin:
                pdf.add_page()
            pdf.ln(1)
            pdf.image(io.BytesIO(annotate(path, visual)), x=pdf.l_margin + 4, w=image_width)
            para(caption(visual), 8, 'I', 80, indent=4, gap=0.3)
        pdf.ln(1.5)

    for key, letter, title, _ in SECTIONS:
        pdf.ln(2)
        para(f'{letter}. {title}', 13, 'B', 25, gap=1)
        entries = sections.get(key, [])
        if key == 'context':
            for aspect, heading in CONTEXT_HEADINGS:
                para(heading, 10.5, 'B', 60, gap=0.5)
                aspect_items = [entry for entry in entries if entry['aspect'] == aspect]
                for entry in aspect_items:
                    item(key, entry)
                if not aspect_items:
                    para(NOT_DISCUSSED, 9, 'I', 110)
            para('Participants', 10.5, 'B', 60, gap=0.5)
            people = sections.get('participants', [])
            for person in people:
                role = f' - {person["role"]}' if person.get('role') else ''
                para(f'- {person["name"]}{role} {references(person, segments_by_id)}', 9.5)
            if not people:
                para('Not identifiable from transcript.', 9, 'I', 110)
            continue
        for entry in entries:
            item(key, entry)
        if not entries:
            para(NOT_DISCUSSED, 9, 'I', 110)

    cited = list(dict.fromkeys(ref for entries in sections.values() for entry in entries for ref in entry.get('source_ids', [])))
    order = list(segments_by_id)
    pdf.ln(2)
    para('Appendix: Cited transcript segments', 13, 'B', 25)
    for ref in sorted((r for r in cited if r in segments_by_id), key=order.index):
        para(f'[{source_label(segments_by_id[ref])}] {segments_by_id[ref]["text"]}', 8.5, '', 50, gap=0.8)
    return bytes(pdf.output())
