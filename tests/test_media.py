import shutil
import subprocess

import pytest

from designnotes import media
from designnotes.transcript import TranscriptError
from tests.conftest import FakeClient

needs_ffmpeg = pytest.mark.skipif(not shutil.which('ffmpeg'), reason='ffmpeg not installed')


def make_tone(path, seconds, video=False):
    command = ['ffmpeg', '-nostdin', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i', f'sine=frequency=440:duration={seconds}']
    if video:
        command += ['-f', 'lavfi', '-i', f'color=c=black:s=64x64:d={seconds}', '-shortest', '-c:v', 'mpeg4']
    subprocess.run(command + [str(path)], check=True)


def test_media_detection():
    assert media.is_media('Meeting.M4A') and media.is_media('call.mp4') and not media.is_media('notes.txt')


@needs_ffmpeg
def test_chunking_offsets_timestamps(tmp_path):
    source = tmp_path / 'meeting.wav'
    make_tone(source, 25)
    chunks = media.prepare_audio(source, tmp_path / 'work', chunk_seconds=10)
    assert len(chunks) == 3
    assert [round(c['start_s']) for c in chunks] == [0, 10, 20]
    client = FakeClient(segments=[{'start': 1.0, 'end': 3.5, 'text': ' Use the M8 bolt.'}])
    doc = media.transcribe_recording(client, source, tmp_path / 'work2', prompt='M8, 6061-T6')
    assert doc['format'] == 'recording'
    assert client.transcriptions[0]['model'] == 'whisper-1' and client.transcriptions[0]['prompt'] == 'M8, 6061-T6'
    assert any('verify numbers' in w for w in doc['warnings'])


@needs_ffmpeg
def test_multi_chunk_transcription(tmp_path, monkeypatch):
    source = tmp_path / 'meeting.mp4'
    make_tone(source, 25, video=True)
    monkeypatch.setattr(media, 'CHUNK_SECONDS', 10)
    monkeypatch.setattr(media.prepare_audio, '__defaults__', (10,))
    client = FakeClient(segments=[{'start': 2.0, 'end': 4.0, 'text': ' Torque is 25 Nm.'}])
    doc = media.transcribe_recording(client, source, tmp_path / 'work')
    assert [s['start_s'] for s in doc['segments']] == pytest.approx([2.0, 12.0, 22.0], abs=0.2)
    assert doc['chunks'] == 3 and doc['segments'][0]['speaker'] is None


@needs_ffmpeg
def test_unreadable_or_silent_input(tmp_path):
    bad = tmp_path / 'broken.mp3'
    bad.write_bytes(b'not audio at all')
    with pytest.raises(TranscriptError, match='could not be read'):
        media.prepare_audio(bad, tmp_path / 'work')
    source = tmp_path / 'tone.wav'
    make_tone(source, 2)
    with pytest.raises(TranscriptError, match='No speech'):
        media.transcribe_recording(FakeClient(segments=[{'start': 0, 'end': 1, 'text': '  '}]), source, tmp_path / 'w2')


def test_missing_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.setattr(media, 'ffmpeg_path', lambda: None)
    with pytest.raises(TranscriptError, match='ffmpeg is required'):
        media.prepare_audio(tmp_path / 'x.mp3', tmp_path)


def test_response_without_segments_falls_back_to_text():
    assert media._segments_from_response({'text': 'Hello'}) == [{'start': 0.0, 'end': None, 'text': 'Hello'}]
