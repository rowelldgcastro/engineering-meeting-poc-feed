"""Recording input: convert an uploaded audio/video recording and transcribe it with timestamps.

ffmpeg converts the recording to compact mono speech audio and splits it into chunks below
the transcription API upload limit. Each chunk is transcribed with Whisper (verbose JSON);
segment times are offset by the chunk start so references point into the original recording.
"""
import csv
import shutil
import subprocess
from pathlib import Path

from designnotes.transcript import TranscriptError, finalize, transcript_document

MEDIA_EXTENSIONS = ('.mp3', '.m4a', '.wav', '.mp4', '.mov', '.webm', '.mkv', '.ogg', '.flac', '.aac', '.mpeg', '.mpga')
CHUNK_SECONDS = 600
API_LIMIT_BYTES = 25 * 1024 * 1024
TRANSCRIPTION_MODEL = 'whisper-1'


def is_media(filename):
    return (filename or '').lower().endswith(MEDIA_EXTENSIONS)


def ffmpeg_path():
    return shutil.which('ffmpeg')


def prepare_audio(source, work_dir, chunk_seconds=CHUNK_SECONDS):
    """Convert to 16 kHz mono MP3 chunks. Returns [{'path', 'start_s', 'end_s'}]."""
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        raise TranscriptError('ffmpeg is required to process recordings. Install it (macOS: brew install ffmpeg) '
                              'or upload a transcript instead.')
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    listing = work_dir / 'chunks.csv'
    command = [ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(source),
               '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'libmp3lame', '-b:a', '48k',
               '-f', 'segment', '-segment_time', str(chunk_seconds), '-reset_timestamps', '1',
               '-segment_list', str(listing), '-segment_list_type', 'csv',
               str(work_dir / 'chunk_%04d.mp3')]
    result = subprocess.run(command, capture_output=True, text=True, timeout=3600)
    if result.returncode != 0 or not listing.exists():
        detail = (result.stderr or '').strip().splitlines()[-1:] or ['no audio stream found']
        raise TranscriptError('The recording could not be read as audio/video: ' + detail[0][:200])
    chunks = []
    with listing.open(newline='') as handle:
        for row in csv.reader(handle):
            if len(row) < 3:
                continue
            path = work_dir / row[0]
            if path.exists() and path.stat().st_size > 0:
                chunks.append({'path': path, 'start_s': float(row[1]), 'end_s': float(row[2])})
    if not chunks:
        raise TranscriptError('The recording contains no audio.')
    for chunk in chunks:
        if chunk['path'].stat().st_size > API_LIMIT_BYTES:
            raise TranscriptError('An audio chunk exceeds the transcription upload limit.')
    return chunks


def _segments_from_response(response):
    raw = getattr(response, 'segments', None)
    if raw is None and isinstance(response, dict):
        raw = response.get('segments')
    result = []
    for segment in raw or []:
        if not isinstance(segment, dict):
            segment = segment.model_dump() if hasattr(segment, 'model_dump') else vars(segment)
        result.append(segment)
    if not result:
        text = getattr(response, 'text', None) or (response.get('text') if isinstance(response, dict) else '')
        if text and text.strip():
            result.append({'start': 0.0, 'end': None, 'text': text})
    return result


def transcribe_recording(client, source, work_dir, prompt=None, language='en', progress=None):
    """Transcribe a recording into a transcript document (same shape as parse_transcript)."""
    chunks = prepare_audio(source, work_dir)
    raw_segments = []
    for number, chunk in enumerate(chunks, start=1):
        if progress:
            progress(f'Transcribing audio part {number} of {len(chunks)}')
        with chunk['path'].open('rb') as audio:
            arguments = {'model': TRANSCRIPTION_MODEL, 'file': audio, 'response_format': 'verbose_json'}
            if language:
                arguments['language'] = language
            if prompt:
                arguments['prompt'] = prompt[:800]
            response = client.audio.transcriptions.create(**arguments)
        duration = chunk['end_s'] - chunk['start_s']
        for segment in _segments_from_response(response):
            text = (segment.get('text') or '').strip()
            if not text:
                continue
            start = max(0.0, min(float(segment.get('start') or 0.0), duration))
            end = segment.get('end')
            end = duration if end is None else max(start, min(float(end), duration))
            raw_segments.append({'start_s': round(chunk['start_s'] + start, 2), 'end_s': round(chunk['start_s'] + end, 2),
                                 'speaker': None, 'text': text, 'line': None})
    segments = finalize(raw_segments)
    if not segments:
        raise TranscriptError('No speech was detected in the recording.')
    document = transcript_document(segments, 'recording')
    document['warnings'].append('Transcribed automatically: verify numbers, units and technical terms against the recording.')
    document['chunks'] = len(chunks)
    return document
