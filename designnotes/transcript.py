"""Transcript input: decode, parse and normalize transcripts into stable source segments.

A segment is the unit every extracted engineering item cites:
    {"id": "S001", "start_s": 12.0 | None, "end_s": float | None,
     "speaker": "Alice" | None, "text": "...", "line": 7}
"""
import io
import re
import zipfile
from xml.etree import ElementTree

MAX_CHARS = 250_000
MAX_SEGMENT_CHARS = 700
SUPPORTED_TEXT_EXTENSIONS = ('.txt', '.md', '.markdown', '.docx', '.srt', '.vtt')


class TranscriptError(ValueError):
    """The transcript cannot be used; the message is safe to show to the user."""


TIME = r'(?:\d{1,2}:)?\d{1,2}:\d{2}(?:[.,]\d{1,3})?'
CUE_TIMING = re.compile(rf'^\s*({TIME})\s*-->\s*({TIME})')
NAME = r"[A-Za-z][\w.'\-]*(?: [A-Za-z][\w.'\-]*){0,3}"
LABELED_PATTERNS = [
    # [00:12:03] Alice: text   |   00:12:03 - Alice: text   |   (12:03) Alice: text
    re.compile(rf'^[\[(]?(?P<time>{TIME})[\])]?\s*(?:[-–—|]\s*)?(?P<speaker>{NAME})\s*:\s*(?P<text>.*)$'),
    # Alice (00:12:03): text   |   Alice [12:03]: text   |   Alice 12:03: text
    re.compile(rf'^(?P<speaker>{NAME})\s*[\[(]?(?P<time>{TIME})[\])]?\s*:\s*(?P<text>.*)$'),
    # Alice: text
    re.compile(rf'^(?P<speaker>{NAME})\s*:\s*(?P<text>.+)$'),
    # [00:12:03] text (timestamp, no speaker)
    re.compile(rf'^[\[(](?P<time>{TIME})[\])]\s*(?:[-–—|]\s*)?(?P<text>.+)$'),
    re.compile(rf'^(?P<time>{TIME})\s+[-–—|]\s+(?P<text>.+)$'),
]
# "Alice Smith  00:03" or "Alice Smith (0:03)" alone on a line, text on following lines (Otter/Teams style)
SPEAKER_HEADER = re.compile(rf'^(?P<speaker>{NAME})\s+[\[(]?(?P<time>{TIME})[\])]?\s*$')
VTT_VOICE = re.compile(r'^<v(?:\.[\w.]+)?\s+([^>]+)>(.*?)(?:</v>)?$')
NOT_SPEAKERS = {
    'note', 'notes', 'decision', 'decisions', 'action', 'actions', 'action item', 'todo', 'to do',
    'question', 'questions', 'answer', 'summary', 'agenda', 'update', 're', 'subject', 'date',
    'time', 'title', 'attendees', 'participants', 'item', 'issue', 'issues', 'result', 'results',
    'conclusion', 'follow up', 'follow-up', 'next steps', 'meeting', 'project', 'location',
    'http', 'https', 'warning', 'example', 'eg', 'ie', 'step', 'goal', 'objective', 'context',
    'transcript', 'recording', 'duration', 'tbd', 'fyi', 'ps', 'nb', 'speaker',
}
MARKDOWN_PREFIX = re.compile(r'^\s*(?:#{1,6}\s+|[-*+]\s+|>\s+|\d+[.)]\s+)')


def parse_time(value):
    parts = value.replace(',', '.').split(':')
    seconds = float(parts[-1])
    minutes = int(parts[-2]) if len(parts) >= 2 else 0
    hours = int(parts[-3]) if len(parts) >= 3 else 0
    return hours * 3600 + minutes * 60 + seconds


def format_time(seconds):
    if seconds is None:
        return None
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f'{hours:02d}:{minutes:02d}:{secs:02d}'


def decode_text(data):
    if not data:
        raise TranscriptError('The transcript is empty.')
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        return data.decode('utf-16')
    if b'\x00' in data[:4096]:
        raise TranscriptError('This file looks binary, not a text transcript. Upload .txt, .md, .docx, .srt or .vtt, or a recording.')
    try:
        return data.decode('utf-8-sig')
    except UnicodeDecodeError:
        return data.decode('cp1252', errors='replace')


def docx_text(data):
    namespace = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xml = archive.read('word/document.xml')
        root = ElementTree.fromstring(xml)
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError, OSError):
        raise TranscriptError('The .docx file could not be read. Re-save it from Word or paste the text instead.') from None
    paragraphs = []
    for paragraph in root.iter(namespace + 'p'):
        pieces = []
        for element in paragraph.iter():
            if element.tag == namespace + 't':
                pieces.append(element.text or '')
            elif element.tag == namespace + 'tab':
                pieces.append('\t')
            elif element.tag in (namespace + 'br', namespace + 'cr'):
                pieces.append('\n')
        paragraphs.append(''.join(pieces))
    return '\n'.join(paragraphs)


def read_upload(filename, data):
    """Return transcript text from an uploaded transcript file."""
    lower = (filename or '').lower()
    if lower.endswith('.docx'):
        return docx_text(data)
    if lower.endswith('.doc'):
        raise TranscriptError('Legacy .doc files are not supported. Save as .docx or paste the text.')
    return decode_text(data)


def clean(text):
    text = text.replace('\r\n', '\n').replace('\r', '\n').replace('\u00a0', ' ')
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)


def strip_markup(line):
    line = MARKDOWN_PREFIX.sub('', line)
    line = re.sub(r'\*\*|__', '', line)
    return line.strip()


def valid_speaker(name):
    name = name.strip()
    return 0 < len(name) <= 40 and name.lower().rstrip('.') not in NOT_SPEAKERS and not name.lower().startswith(('http', 'www'))


def split_long(text):
    """Split very long passages at sentence boundaries so citations stay specific."""
    if len(text) <= MAX_SEGMENT_CHARS:
        return [text]
    sentences = re.split(r'(?<=[.!?])\s+', text)
    pieces, current = [], ''
    for sentence in sentences:
        while len(sentence) > MAX_SEGMENT_CHARS:
            cut = sentence.rfind(' ', 0, MAX_SEGMENT_CHARS)
            cut = cut if cut > 0 else MAX_SEGMENT_CHARS
            if current:
                pieces.append(current)
                current = ''
            pieces.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if current and len(current) + 1 + len(sentence) > MAX_SEGMENT_CHARS:
            pieces.append(current)
            current = sentence
        else:
            current = (current + ' ' + sentence).strip()
    if current:
        pieces.append(current)
    return [piece for piece in pieces if piece]


def parse_cues(lines):
    """SRT / WebVTT: timing line followed by cue text."""
    raw, index = [], 0
    while index < len(lines):
        match = CUE_TIMING.match(lines[index])
        if not match:
            index += 1
            continue
        start, end, line_number = parse_time(match.group(1)), parse_time(match.group(2)), index + 1
        index += 1
        text_lines = []
        while index < len(lines) and lines[index].strip() and not CUE_TIMING.match(lines[index]):
            text_lines.append(lines[index].strip())
            index += 1
        speaker, parts = None, []
        for text_line in text_lines:
            voice = VTT_VOICE.match(text_line)
            if voice:
                speaker, text_line = voice.group(1).strip(), voice.group(2)
            parts.append(re.sub(r'<[^>]+>', '', text_line).strip())
        text = ' '.join(part for part in parts if part)
        labeled = re.match(rf'^({NAME})\s*:\s*(.+)$', text)
        if not speaker and labeled and valid_speaker(labeled.group(1)):
            speaker, text = labeled.group(1), labeled.group(2)
        if text:
            raw.append({'start_s': start, 'end_s': end, 'speaker': speaker, 'text': text, 'line': line_number})
    # Drop SRT numeric counters accidentally captured as text.
    return [segment for segment in raw if not segment['text'].isdigit()]


def match_line(line):
    for pattern in LABELED_PATTERNS:
        match = pattern.match(line)
        if match:
            groups = match.groupdict()
            speaker = groups.get('speaker')
            if speaker is not None and not valid_speaker(speaker):
                continue
            return speaker, groups.get('time'), groups['text'].strip()
    return None


def parse_lines(lines):
    """Free-form transcripts with optional speaker labels and timestamps."""
    entries = []  # (line_number, speaker|None, time|None, text, labeled: bool)
    pending_header = None
    for number, original in enumerate(lines, start=1):
        line = strip_markup(original)
        if not line:
            entries.append((number, None, None, '', False))
            continue
        header = SPEAKER_HEADER.match(line)
        if header and valid_speaker(header.group('speaker')):
            pending_header = (header.group('speaker'), header.group('time'))
            continue
        matched = match_line(line)
        if pending_header and not (matched and matched[0]):
            entries.append((number, pending_header[0], pending_header[1], line, True))
            pending_header = None
            continue
        pending_header = None
        if matched:
            speaker, time, text = matched
            entries.append((number, speaker, time, text, True))
        else:
            entries.append((number, None, None, line, False))

    labeled_speakers = [speaker for _, speaker, _, _, labeled in entries if labeled and speaker]
    use_speakers = len(labeled_speakers) >= 2
    use_times = sum(1 for _, _, time, _, _ in entries if time) >= 2

    segments, current = [], None
    for number, speaker, time, text, labeled in entries:
        starts_new = labeled and ((use_speakers and speaker) or (use_times and time))
        if not text:
            continue
        if not use_speakers and labeled and speaker:
            # Too few labels to trust: keep the original wording intact.
            text = strip_markup(lines[number - 1])
        if starts_new:
            if current:
                segments.append(current)
            current = {'start_s': parse_time(time) if (use_times and time) else None, 'end_s': None,
                       'speaker': speaker if use_speakers else None, 'text': text, 'line': number, 'labeled': True}
        elif current and current['labeled']:
            current['text'] += ' ' + text
        else:
            # Unlabeled lines are kept as separate segments for precise citations.
            if current:
                segments.append(current)
            current = {'start_s': None, 'end_s': None, 'speaker': None, 'text': text, 'line': number, 'labeled': False}
    if current:
        segments.append(current)
    for segment in segments:
        segment.pop('labeled')
    return segments


def finalize(raw_segments):
    segments = []
    for segment in raw_segments:
        text = re.sub(r'\s+', ' ', segment['text']).strip()
        # Lines with no words (bare timestamps, cue numbers, rules) are not content unless a speaker said them.
        if not text or not re.search(r'[A-Za-z]' if not segment['speaker'] else r'[A-Za-z0-9]', text):
            continue
        for piece in split_long(text):
            segments.append({**segment, 'text': piece})
    # Fill missing end times from the next timed segment.
    for current, following in zip(segments, segments[1:]):
        if current['start_s'] is not None and current['end_s'] is None and following['start_s'] is not None:
            current['end_s'] = max(current['start_s'], following['start_s'])
    width = max(3, len(str(len(segments))))
    return [{'id': f'S{index:0{width}d}', 'start_s': s['start_s'], 'end_s': s['end_s'],
             'speaker': s['speaker'], 'text': s['text'], 'line': s['line']}
            for index, s in enumerate(segments, start=1)]


def parse_transcript(text):
    """Parse transcript text into a normalized transcript document."""
    if not isinstance(text, str):
        raise TranscriptError('Transcript text must be a string.')
    text = clean(text)
    if not text.strip():
        raise TranscriptError('The transcript is empty.')
    if len(text) > MAX_CHARS:
        raise TranscriptError(f'The transcript has {len(text):,} characters; the limit is {MAX_CHARS:,} '
                              '(about 4 hours of speech). Split it into separate meetings.')
    lines = text.split('\n')
    cue_lines = sum(1 for line in lines if CUE_TIMING.match(line))
    if cue_lines >= 1 and cue_lines >= len([l for l in lines if l.strip()]) * 0.15:
        fmt, raw = ('vtt' if text.lstrip().startswith('WEBVTT') else 'srt'), parse_cues(lines)
    else:
        fmt, raw = 'text', parse_lines(lines)
    segments = finalize(raw)
    if not segments:
        raise TranscriptError('No readable transcript text was found (only timestamps, numbers or symbols).')
    return transcript_document(segments, fmt)


def transcript_document(segments, fmt):
    speakers = []
    for segment in segments:
        if segment['speaker'] and segment['speaker'] not in speakers:
            speakers.append(segment['speaker'])
    warnings = []
    if not speakers:
        warnings.append('No speaker labels found; participants cannot be attributed from the transcript.')
    if all(s['start_s'] is None for s in segments):
        warnings.append('No timestamps found; source references use segment IDs only.')
    return {'format': fmt, 'segments': segments, 'speakers': speakers,
            'has_timestamps': any(s['start_s'] is not None for s in segments),
            'char_count': sum(len(s['text']) for s in segments), 'warnings': warnings}


def source_label(segment):
    """Human-readable reference, e.g. 'S014 · 00:03:15 · Alice'."""
    parts = [segment['id']]
    if segment.get('start_s') is not None:
        parts.append(format_time(segment['start_s']))
    if segment.get('speaker'):
        parts.append(segment['speaker'])
    return ' · '.join(parts)
