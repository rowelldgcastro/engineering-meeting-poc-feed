"""Notes service: persistence and background processing of recording/transcript jobs."""
import copy
import json
import os
import queue
import re
import shutil
import sys
import threading
import time
import uuid
from pathlib import Path

from designnotes import extraction, media
from designnotes.pdf import render_pdf
from designnotes.report import render_markdown
from designnotes.transcript import SUPPORTED_TEXT_EXTENSIONS, TranscriptError, parse_transcript, read_upload

MAX_UPLOAD_BYTES = 2 * 1024 ** 3
MAX_TRANSCRIPT_UPLOAD_BYTES = 20 * 1024 ** 2
ACTIVE_STATES = ('queued', 'transcribing', 'extracting')


def default_root():
    override = os.getenv('DESIGNNOTES_DATA_DIR')
    if override:
        return Path(override)
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'Engineering Design Notes'
    return Path.home() / '.local' / 'share' / 'engineering-design-notes'


def write_json(path, data):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, indent=2), encoding='utf-8')
    temporary.replace(path)


def clean_field(value, name, limit, required=False):
    if value is None:
        value = ''
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f'{name} must be text of at most {limit} characters.')
    value = value.strip()
    if required and not value:
        raise ValueError(f'{name} is required.')
    return value or None


class NotesService:
    def __init__(self, root=None, client_factory=None, start_worker=True):
        self.root = Path(root) if root is not None else default_root()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.client_factory = client_factory
        self.key = os.getenv('OPENAI_API_KEY', '')
        self.records = {}
        self.jobs = queue.Queue()
        for path in self.root.glob('*/record.json'):
            try:
                record = json.loads(path.read_text(encoding='utf-8'))
                if not re.fullmatch(r'[a-f0-9]{32}', record['id']):
                    continue
                if record['state'] in ACTIVE_STATES:
                    record['state'] = 'failed'
                    record['error'] = 'The app stopped while processing. Use Retry.'
                    write_json(path, record)
                self.records[record['id']] = record
            except (OSError, ValueError, KeyError):
                continue
        self.worker = None
        if start_worker:
            self.worker = threading.Thread(target=self.work_loop, daemon=True)
            self.worker.start()

    # ------------------------------------------------------------ helpers
    def directory(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r'[a-f0-9]{32}', identifier):
            raise ValueError('Invalid notes ID.')
        return self.root / identifier

    def save(self, record):
        write_json(self.directory(record['id']) / 'record.json', record)

    def get(self, identifier):
        self.directory(identifier)
        if identifier not in self.records:
            raise KeyError('Unknown notes.')
        return self.records[identifier]

    def make_client(self):
        if self.client_factory:
            return self.client_factory()
        if not self.key:
            raise ValueError('Enter your OpenAI API key first. It is kept in memory only.')
        from openai import OpenAI
        return OpenAI(api_key=self.key, timeout=600, max_retries=2)

    def set_key(self, value):
        if not isinstance(value, str) or not value.strip() or len(value) > 1000:
            raise ValueError('Invalid API key.')
        self.key = value.strip()

    def status(self):
        return {'api_key_set': bool(self.key or self.client_factory), 'ffmpeg': bool(media.ffmpeg_path()),
                'model': extraction.MODEL, 'transcription_model': media.TRANSCRIPTION_MODEL,
                'media_extensions': list(media.MEDIA_EXTENSIONS),
                'transcript_extensions': list(SUPPORTED_TEXT_EXTENSIONS),
                'sections': [{'key': k, 'letter': l, 'title': t, 'description': d} for k, l, t, d in extraction.SECTIONS]}

    def list(self):
        with self.lock:
            return [{'id': r['id'], 'title': r['title'], 'state': r['state'], 'created_at': r['created_at'],
                     'approved': bool(r['review'].get('approved_at')), 'source_kind': r['source']['kind']}
                    for r in sorted(self.records.values(), key=lambda r: r['created_at'], reverse=True)]

    def snapshot(self, identifier):
        with self.lock:
            return copy.deepcopy(self.get(identifier))

    def _new_record(self, title, project, glossary, source):
        identifier = uuid.uuid4().hex
        self.directory(identifier).mkdir(mode=0o700)
        return {'id': identifier, 'title': title or 'Engineering design notes', 'project': project,
                'glossary': glossary, 'created_at': time.time(), 'state': 'queued', 'progress': 'Queued',
                'error': None, 'source': source, 'transcript': None, 'notes': None,
                'review': {'approved_at': None, 'edited_at': None}, 'usage': {}}

    # ------------------------------------------------------------ creation
    def create_from_text(self, text, title=None, project=None):
        title = clean_field(title, 'Title', 200)
        project = clean_field(project, 'Project/component', 200)
        transcript = parse_transcript(text)
        return self._enqueue_transcript(transcript, title, project, {'kind': 'paste', 'filename': None,
                                                                      'size': len(text.encode('utf-8'))}, text)

    def create_from_upload(self, filename, stream, length, title=None, project=None, glossary=None):
        filename = clean_field(filename, 'File name', 255, required=True)
        title = clean_field(title, 'Title', 200)
        project = clean_field(project, 'Project/component', 200)
        glossary = clean_field(glossary, 'Glossary', 800)
        safe_name = re.sub(r'[^\w.\- ]', '_', os.path.basename(filename))[:120] or 'upload'
        lower = safe_name.lower()
        if not isinstance(length, int) or length <= 0:
            raise ValueError('The uploaded file is empty.')
        if media.is_media(lower):
            if length > MAX_UPLOAD_BYTES:
                raise ValueError('Recordings are limited to 2 GB.')
            if not media.ffmpeg_path():
                raise ValueError('ffmpeg is required to process recordings. Install it (macOS: brew install ffmpeg) '
                                 'or upload a transcript instead.')
            self.make_client()
            with self.lock:
                record = self._new_record(title or os.path.splitext(safe_name)[0], project, glossary,
                                          {'kind': 'recording', 'filename': safe_name, 'size': length})
                destination = self.directory(record['id']) / ('source' + os.path.splitext(lower)[1])
            self._copy(stream, length, destination)
            with self.lock:
                record['source']['path'] = destination.name
                self.records[record['id']] = record
                self.save(record)
            self.jobs.put(record['id'])
            return record['id']
        if lower.endswith(SUPPORTED_TEXT_EXTENSIONS):
            if length > MAX_TRANSCRIPT_UPLOAD_BYTES:
                raise ValueError('Transcript files are limited to 20 MB.')
            data = stream.read(length)
            transcript = parse_transcript(read_upload(lower, data))
            return self._enqueue_transcript(transcript, title or os.path.splitext(safe_name)[0], project,
                                            {'kind': 'transcript', 'filename': safe_name, 'size': length}, None, data, lower)
        raise ValueError('Unsupported file type. Upload a recording (' + ', '.join(media.MEDIA_EXTENSIONS) +
                         ') or a transcript (' + ', '.join(SUPPORTED_TEXT_EXTENSIONS) + ').')

    def _copy(self, stream, length, destination):
        remaining = length
        with destination.open('wb') as handle:
            while remaining > 0:
                block = stream.read(min(1024 * 1024, remaining))
                if not block:
                    raise ValueError('The upload was interrupted.')
                handle.write(block)
                remaining -= len(block)

    def _enqueue_transcript(self, transcript, title, project, source, text=None, data=None, name=None):
        self.make_client()
        with self.lock:
            record = self._new_record(title, project, None, source)
            directory = self.directory(record['id'])
            if text is not None:
                (directory / 'source.txt').write_text(text, encoding='utf-8')
            if data is not None:
                (directory / ('source' + os.path.splitext(name)[1])).write_bytes(data)
            record['transcript'] = transcript
            self.records[record['id']] = record
            self.save(record)
        self.jobs.put(record['id'])
        return record['id']

    # ------------------------------------------------------------ processing
    def work_loop(self):
        while True:
            identifier = self.jobs.get()
            if identifier is None:
                return
            self.process(identifier)

    def _update(self, record, **values):
        with self.lock:
            record.update(values)
            self.save(record)

    def process(self, identifier):
        record = self.records[identifier]
        directory = self.directory(identifier)
        try:
            client = self.make_client()
            if record['transcript'] is None:
                self._update(record, state='transcribing', progress='Converting recording audio', error=None)
                transcript = media.transcribe_recording(
                    client, directory / record['source']['path'], directory / 'audio', prompt=record.get('glossary'),
                    progress=lambda message: self._update(record, progress=message))
                shutil.rmtree(directory / 'audio', ignore_errors=True)
                self._update(record, transcript=transcript)
            self._update(record, state='extracting', progress='Extracting engineering information', error=None)
            notes, raw, usage = extraction.extract(client, record['transcript'], record['title'], record['project'])
            (directory / 'model_response.json').write_text(raw, encoding='utf-8')
            self._update(record, notes=notes, usage=usage, state='ready', progress='Ready for review',
                         review={'approved_at': None, 'edited_at': None})
        except (TranscriptError, extraction.ExtractionError, ValueError) as error:
            self._update(record, state='failed', progress='Failed', error=str(error))
        except Exception as error:  # network/API failures
            message = getattr(error, 'message', None) or type(error).__name__
            self._update(record, state='failed', progress='Failed',
                         error=f'Processing failed ({message}). Your upload is kept; check the API key/network and Retry.')

    def retry(self, identifier):
        with self.lock:
            record = self.get(identifier)
            if record['state'] != 'failed':
                raise ValueError('Only failed notes can be retried.')
            if record['transcript'] is None and not record['source'].get('path'):
                raise ValueError('The original upload is no longer available.')
            self.make_client()
            record.update(state='queued', progress='Queued', error=None)
            self.save(record)
        self.jobs.put(identifier)

    # ------------------------------------------------------------ review
    def _ready(self, identifier):
        record = self.get(identifier)
        if record['state'] != 'ready' or not record['notes']:
            raise ValueError('Notes are not ready yet.')
        return record

    def edit(self, identifier, section, item_id, text=None, delete=False):
        with self.lock:
            record = self._ready(identifier)
            items = record['notes']['sections'].get(section)
            if items is None:
                raise ValueError('Unknown section.')
            matches = [item for item in items if item['id'] == item_id]
            if not matches:
                raise ValueError('Unknown item.')
            if delete:
                items.remove(matches[0])
            else:
                field = 'name' if section == 'participants' else 'text'
                value = clean_field(text, 'Item text', extraction.MAX_TEXT, required=True)
                if value != matches[0][field]:
                    matches[0][field] = value
                    matches[0]['edited'] = True
            record['review'] = {'approved_at': None, 'edited_at': time.time()}
            self.save(record)

    def rename(self, identifier, title, project=None):
        with self.lock:
            record = self._ready(identifier)
            record['title'] = record['notes']['title'] = clean_field(title, 'Title', 200, required=True)
            record['project'] = record['notes']['project'] = clean_field(project, 'Project/component', 200)
            record['review'] = {'approved_at': None, 'edited_at': time.time()}
            self.save(record)

    def approve(self, identifier, approved=True):
        with self.lock:
            record = self._ready(identifier)
            record['review']['approved_at'] = time.time() if approved else None
            self.save(record)

    def delete(self, identifier):
        with self.lock:
            record = self.get(identifier)
            if record['state'] in ACTIVE_STATES:
                raise ValueError('Wait for processing to finish before deleting.')
            shutil.rmtree(self.directory(identifier), ignore_errors=True)
            del self.records[identifier]

    def export_markdown(self, identifier):
        with self.lock:
            return render_markdown(copy.deepcopy(self._ready(identifier)))

    def export_pdf(self, identifier):
        with self.lock:
            record = copy.deepcopy(self._ready(identifier))
        return render_pdf(record)
