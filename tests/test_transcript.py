import io
import zipfile

import pytest

from designnotes.transcript import MAX_CHARS, TranscriptError, parse_transcript, read_upload, source_label
from tests.conftest import TRANSCRIPT


def test_labeled_timestamped_transcript():
    doc = parse_transcript(TRANSCRIPT)
    assert [s['id'] for s in doc['segments']] == ['S001', 'S002', 'S003', 'S004', 'S005']
    assert doc['speakers'] == ['Alice', 'Bob']
    assert doc['segments'][1]['start_s'] == 20.0 and doc['segments'][1]['end_s'] == 62.0
    assert '6061-T6 aluminium at 3.2 mm' in doc['segments'][1]['text']
    assert source_label(doc['segments'][2]) == 'S003 · 00:01:02 · Alice'


@pytest.mark.parametrize('text,speaker,start', [
    ('Alice (00:01:05): one\nBob (00:01:09): two', 'Alice', 65.0),
    ('00:01:05 - Alice: one\n00:01:09 - Bob: two', 'Alice', 65.0),
    ('Alice Smith  1:05\none\nBob Jones  1:09\ntwo', 'Alice Smith', 65.0),
    ('**Alice:** one\n**Bob:** two', 'Alice', None),
    ('1\n00:01:05,000 --> 00:01:08,000\nAlice: one\n\n2\n00:01:09,000 --> 00:01:10,000\nBob: two', 'Alice', 65.0),
    ('WEBVTT\n\n01:05.000 --> 01:08.000\n<v Alice>one</v>\n\n01:09.000 --> 01:10.000\n<v Bob>two</v>', 'Alice', 65.0),
])
def test_common_formats(text, speaker, start):
    first = parse_transcript(text)['segments'][0]
    assert first['speaker'] == speaker and first['start_s'] == start and first['text'] == 'one'


def test_continuation_lines_join_previous_speaker():
    doc = parse_transcript('Alice: The bracket\nneeds two M8 bolts.\nBob: Fine.')
    assert doc['segments'][0]['text'] == 'The bracket needs two M8 bolts.'


def test_unlabeled_transcript_keeps_text_and_warns():
    doc = parse_transcript('We discussed the gearbox.\nDecision: keep the 3:1 ratio.\n\nNo timestamps here.')
    assert [s['speaker'] for s in doc['segments']] == [None, None, None]
    assert doc['segments'][1]['text'] == 'Decision: keep the 3:1 ratio.'
    assert any('No speaker labels' in w for w in doc['warnings'])
    assert any('No timestamps' in w for w in doc['warnings'])


def test_keyword_labels_are_not_speakers():
    doc = parse_transcript('Note: draft only\nAction: check torque\nAlice: hi\nBob: hello')
    assert doc['speakers'] == ['Alice', 'Bob']
    assert doc['segments'][0]['speaker'] is None


@pytest.mark.parametrize('text', ['', '   \n\t  ', '[00:00:01]\n[00:00:02]\n---', '1\n2\n3'])
def test_empty_or_contentless(text):
    with pytest.raises(TranscriptError):
        parse_transcript(text)


def test_binary_and_bad_docx_rejected():
    with pytest.raises(TranscriptError):
        read_upload('notes.txt', b'\x00\x01\x02binary')
    with pytest.raises(TranscriptError):
        read_upload('notes.docx', b'not a zip')
    with pytest.raises(TranscriptError):
        read_upload('notes.doc', b'x')


def test_docx_extraction():
    xml = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
           '<w:p><w:r><w:t>Alice: Use M8 bolts.</w:t></w:r></w:p><w:p><w:r><w:t>Bob: Agreed.</w:t></w:r></w:p>'
           '</w:body></w:document>')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('word/document.xml', xml)
    doc = parse_transcript(read_upload('t.docx', buffer.getvalue()))
    assert [(s['speaker'], s['text']) for s in doc['segments']] == [('Alice', 'Use M8 bolts.'), ('Bob', 'Agreed.')]


def test_utf16_and_cp1252_decoding():
    assert parse_transcript(read_upload('a.txt', 'Alice: 5 µm ± 0.1\nBob: ok'.encode('utf-16')))['segments'][0]['text'] == '5 µm ± 0.1'
    assert 'café' in parse_transcript(read_upload('a.txt', 'Alice: café\nBob: ok'.encode('cp1252')))['segments'][0]['text']


def test_lengthy_transcript_split_and_limit():
    line = 'Alice: ' + ' '.join(['The shaft runs at 1500 rpm.'] * 60)
    doc = parse_transcript(line + '\nBob: ok')
    assert all(len(s['text']) <= 700 for s in doc['segments'])
    assert len(doc['segments']) > 3
    long_ok = '\n'.join(f'[{i // 60:02d}:{i % 60:02d}] Speaker{i % 3}: Segment {i} discusses torque of {i} Nm.' for i in range(3000))
    assert len(parse_transcript(long_ok)['segments']) == 3000
    with pytest.raises(TranscriptError, match='limit'):
        parse_transcript('a' * (MAX_CHARS + 1))
