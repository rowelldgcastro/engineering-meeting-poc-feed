from designnotes.extraction import build_notes
from designnotes.pdf import render_pdf
from designnotes.report import render_markdown
from designnotes.summary import OTHER_TOPIC, all_bullets, build_summary, bullet_visuals, summarize
from designnotes.transcript import parse_transcript
from tests.conftest import TRANSCRIPT, FakeClient, sample_payload, sample_summary


def notes_and_segments():
    segments = parse_transcript(TRANSCRIPT)['segments']
    return build_notes(sample_payload(), segments, 'Bracket review'), segments


def test_summary_cites_items_and_keeps_uncited_items():
    notes, segments = notes_and_segments()
    client = FakeClient()
    concise, raw, _ = summarize(client, notes, {'segments': segments})
    request = client.requests[0]
    assert request['response_format']['json_schema']['name'] == 'meeting_summary'
    assert '"id": "D1"' in request['messages'][1]['content']
    first = next(b for b in concise['topics'][0]['bullets'] if b['item_ids'][:1] == ['D1'])
    assert first['item_ids'] == ['D1', 'A1'] and first['source_ids'] == ['S002', 'S003']
    assert first['children'][0]['text'] == 'Weight prioritized over cost' and not first['flags']
    assert concise['next_steps'][0]['source_ids'] == ['S005']
    assert [t['title'] for t in concise['topics']] == ['Bracket material']
    cited = {i for b in all_bullets(concise) for i in b['item_ids']}
    assert {'C1', 'L1'} <= cited and 'C1' == concise['topics'][0]['bullets'][0]['item_ids'][0]


def test_leftovers_without_topics_go_to_other_points():
    notes, segments = notes_and_segments()
    concise = build_summary({'topics': [], 'next_steps': []}, notes, segments)
    assert [t['title'] for t in concise['topics']] == [OTHER_TOPIC]


def test_summary_drops_unsupported_bullets_and_flags_new_values():
    notes, segments = notes_and_segments()
    payload = sample_summary()
    payload['topics'][0]['bullets'] += [{'text': 'Invented point', 'item_ids': ['X9'], 'sub_bullets': []},
                                        {'text': 'Bracket thickness 4.5 mm', 'item_ids': ['D1'], 'sub_bullets': []}]
    concise = build_summary(payload, notes, segments)
    texts = [b['text'] for b in concise['topics'][0]['bullets']]
    assert 'Invented point' not in texts
    flagged = next(b for b in concise['topics'][0]['bullets'] if b['text'].startswith('Bracket thickness'))
    assert flagged['flags'][0]['code'] == 'unsupported_value' and '4.5' in flagged['flags'][0]['message']


def test_each_screen_moment_is_shown_once():
    notes, segments = notes_and_segments()
    shot = {'file': 'D1.jpg', 'pointer_id': 'P001', 'bbox': None, 'cursor_source': 'pointing', 'confidence': 'high'}
    notes['sections']['decisions'][0]['visuals'] = [shot]
    notes['sections']['rationale'][0]['visuals'] = [dict(shot, file='R1.jpg')]
    concise = build_summary(sample_summary(), notes, segments)
    chosen = bullet_visuals(concise, notes)
    assert list(chosen.values()) == [shot]


def test_exports_lead_with_concise_notes():
    notes, segments = notes_and_segments()
    notes['summary'] = build_summary(sample_summary(), notes, segments)
    record = {'id': 'a' * 32, 'title': 'Bracket review', 'project': None, 'created_at': 1_700_000_000,
              'source': {'kind': 'transcript', 'filename': 'review.txt'}, 'transcript': parse_transcript(TRANSCRIPT),
              'notes': notes, 'review': {'approved_at': None, 'edited_at': None}, 'state': 'ready'}
    md = render_markdown(record)
    assert md.index('## Bracket material') < md.index('# Detailed engineering record') < md.index('## A. Engineering Context')
    assert '- Decided: 6061-T6 aluminium at 3.2 mm `00:00:20-00:01:02`' in md
    assert '  - Weight prioritized over cost `00:01:02`' in md and '## Next steps' in md
    assert render_pdf(record).startswith(b'%PDF')
