from designnotes.extraction import build_notes
from designnotes.pdf import render_pdf
from designnotes.report import render_markdown
from designnotes.transcript import parse_transcript
from tests.conftest import TRANSCRIPT, sample_payload


def make_record(payload=None):
    transcript = parse_transcript(TRANSCRIPT)
    notes = build_notes(sample_payload() if payload is None else payload, transcript['segments'], 'Bracket review', 'P-200')
    return {'id': 'a' * 32, 'title': 'Bracket review', 'project': 'P-200', 'created_at': 1_700_000_000,
            'source': {'kind': 'transcript', 'filename': 'review.txt'}, 'transcript': transcript, 'notes': notes,
            'review': {'approved_at': None, 'edited_at': None}, 'state': 'ready'}


def test_markdown_sections_sources_and_values():
    md = render_markdown(make_record())
    for heading in ['A. Engineering Context', 'B. Engineering Decisions', 'C. Design Alternatives',
                    'D. Engineering Rationale and Trade-offs', 'E. Supporting Evidence',
                    'F. Open Issues and Follow-up Actions', 'G. Lessons Learned']:
        assert '## ' + heading in md
    assert 'Use 6061-T6 aluminium at 3.2 mm' in md
    assert '[S003 · 00:01:02 · Alice]' in md
    assert md.count('Not discussed in transcript') >= 1
    assert 'DRAFT' in md.upper()


def test_empty_notes_say_not_discussed():
    payload = {k: [] for k in sample_payload()}
    md = render_markdown(make_record(payload))
    assert md.count('Not discussed in transcript') >= 7


def test_pdf_renders_unicode_safely():
    record = make_record()
    record['notes']['sections']['decisions'][0]['text'] = 'Tolerance ±0.05 mm, 20 °C, µm “quoted” — ≤ 5 Ω → ok'
    record['review']['approved_at'] = 1_700_000_500
    pdf = render_pdf(record)
    assert pdf.startswith(b'%PDF') and len(pdf) > 2000
