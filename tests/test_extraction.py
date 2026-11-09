import json

import pytest

from designnotes.extraction import EXTRACTION_SCHEMA, ExtractionError, build_notes, extract, normalize, numbers_in, quote_in
from designnotes.transcript import parse_transcript
from tests.conftest import TRANSCRIPT, FakeClient, item, sample_payload


@pytest.fixture
def segments():
    return parse_transcript(TRANSCRIPT)['segments']


def empty_payload(**sections):
    payload = {key: [] for key in EXTRACTION_SCHEMA['required']}
    payload.update(sections)
    return payload


def flags(entry):
    return {flag['code'] for flag in entry['flags']}


def test_schema_is_strict():
    def check(schema):
        if schema.get('type') == 'object':
            assert schema['additionalProperties'] is False
            assert set(schema['required']) == set(schema['properties'])
            for child in schema['properties'].values():
                check(child)
        if schema.get('type') == 'array':
            check(schema['items'])
    check(EXTRACTION_SCHEMA)


def test_valid_payload_verified_without_warnings(segments):
    notes = build_notes(sample_payload(), segments, 'Review', 'P-200')
    sections = notes['sections']
    assert [i['id'] for i in sections['decisions']] == ['D1']
    assert not any(f['level'] == 'warning' for items in sections.values() for i in items for f in i['flags'])
    assert sections['open_issues'][0]['owner'] == 'Alice' and sections['open_issues'][0]['due'] == 'Friday'
    assert notes['stats'] == {'items': 8, 'flagged': 0, 'dropped': 0}


def test_unsupported_items_are_dropped_or_flagged(segments):
    payload = empty_payload(decisions=[
        item(text='Invented decision', quote='we chose titanium', source_ids=['S099'], parameters=[], rationale=None),
        item(text='Use mild steel at 2 mm', quote='mild steel at 2 mm', source_ids=['S002'], parameters=['2 mm'], rationale=None),
        item(text='Use aluminium at 4.5 mm', quote="let's go with the aluminium", source_ids=['S003'], parameters=['4.5 mm'], rationale=None),
        item(text='Paraphrased', quote='we picked the light one', source_ids=['S003'], parameters=[], rationale=None),
    ])
    notes = build_notes(payload, segments)
    decisions = notes['sections']['decisions']
    assert [d['text'] for d in decisions] == ['Use mild steel at 2 mm', 'Use aluminium at 4.5 mm', 'Paraphrased']
    assert 'decision_not_explicit' in flags(decisions[0])
    assert 'unsupported_value' in flags(decisions[1]) and '4.5' in decisions[1]['flags'][0]['message']
    assert 'quote_not_found' in flags(decisions[2]) and decisions[2]['basis'] == 'interpreted'
    assert notes['dropped'][0]['text'] == 'Invented decision'


def test_quote_outside_citation_corrects_source(segments):
    payload = empty_payload(lessons=[item(text='Steel bracket cracked', quote='steel bracket cracked at the weld', source_ids=['S001'])])
    lesson = build_notes(payload, segments)['sections']['lessons'][0]
    assert lesson['source_ids'] == ['S001', 'S005'] and 'source_extended' in flags(lesson)


def test_values_in_adjacent_segments_extend_sources(segments):
    payload = empty_payload(decisions=[item(text='Aluminium 6061-T6 at 3.2 mm', quote="let's go with the aluminium",
                                            source_ids=['S003'], parameters=['6061-T6'], rationale=None)])
    decision = build_notes(payload, segments)['sections']['decisions'][0]
    assert decision['source_ids'] == ['S002', 'S003'] and 'unsupported_value' not in flags(decision)


def test_owner_and_due_must_be_stated(segments):
    payload = empty_payload(open_issues=[item(text='Fatigue check', quote='Who checks the fatigue life?', source_ids=['S004'],
                                              kind='question', owner='Bob', due='Monday')])
    issue = build_notes(payload, segments)['sections']['open_issues'][0]
    assert issue['owner'] is None and issue['due'] is None
    assert {'owner_removed', 'due_removed'} <= flags(issue)


def test_selected_alternative_requires_selection_wording(segments):
    payload = empty_payload(alternatives=[item(text='Steel', quote='mild steel at 2 mm', source_ids=['S002'], option='Steel',
                                               advantages=[], disadvantages=[], outcome='selected', outcome_reason=None)])
    assert 'selection_not_explicit' in flags(build_notes(payload, segments)['sections']['alternatives'][0])


def test_participants_must_exist_and_roles_must_be_stated(segments):
    payload = empty_payload(participants=[{'name': 'Alice', 'role': 'chief engineer', 'source_ids': []},
                                          {'name': 'Mallory', 'role': None, 'source_ids': ['S001']}])
    notes = build_notes(payload, segments)
    assert [(p['name'], p['role']) for p in notes['sections']['participants']] == [('Alice', None)]


def test_duplicates_merged(segments):
    entry = item(text='Weight matters most', quote='weight matters most', source_ids=['S003'], kind='rationale', factors=['weight'])
    notes = build_notes(empty_payload(rationale=[entry, dict(entry, text='weight matters most.')]), segments)
    assert len(notes['sections']['rationale']) == 1


@pytest.mark.parametrize('payload', ['not json', '[]', json.dumps({'context': []})])
def test_malformed_payload_rejected(segments, payload):
    with pytest.raises(ExtractionError):
        build_notes(payload, segments)


def test_malformed_items_reported_not_passed_through(segments):
    payload = empty_payload(evidence=[{'text': 'x'}, item(text='40% heavier', quote='40% heavier', source_ids=['S002'], kind='bogus')])
    notes = build_notes(payload, segments)
    assert notes['sections']['evidence'] == [] and len(notes['dropped']) == 2


def test_extract_uses_strict_schema_and_handles_truncation(segments):
    client = FakeClient()
    notes, raw, _ = extract(client, {'segments': segments}, 'Title', 'Project')
    request = client.requests[0]
    assert request['response_format']['json_schema']['strict'] is True and request['temperature'] == 0
    assert 'S003 [00:01:02] Alice:' in request['messages'][1]['content']
    assert notes['title'] == 'Title' and json.loads(raw)
    with pytest.raises(ExtractionError, match='incomplete'):
        extract(FakeClient(finish_reason='length'), {'segments': segments})


def test_helpers():
    assert quote_in('the “aluminium” – 3.2 mm', 'We use the "aluminium" - 3.2 mm here.')
    assert quote_in('we use ... 3.2 mm', 'We use the aluminium at 3.2 mm.')
    assert not quote_in('3.5 mm', 'We use 3.2 mm.')
    assert {'1200', '0.5', '.5', '3'} <= numbers_in('1,200 rpm, 0.5 mm and three bolts')
    assert normalize('M8 × 1.25, “Grade 8.8”.') == 'm8 1.25 grade 8.8'
