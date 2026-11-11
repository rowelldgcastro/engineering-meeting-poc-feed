import http.client
import json
import shutil
import subprocess
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

from designnotes.server import handler_factory
from designnotes.service import NotesService
from tests.conftest import TRANSCRIPT, FakeClient

TOKEN = 'test-token'


def wait_ready(service, identifier, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = service.snapshot(identifier)['state']
        if state in ('ready', 'failed'):
            return service.snapshot(identifier)
        time.sleep(0.05)
    raise AssertionError('processing timed out')


@pytest.fixture
def service(tmp_path):
    return NotesService(tmp_path / 'data', client_factory=FakeClient)


@pytest.fixture
def server(service):
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), handler_factory(service, TOKEN))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def request(server, method, path, body=None, headers=None, token=TOKEN, raw=False):
    connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=20)
    all_headers = {'Host': f'127.0.0.1:{server.server_port}'}
    if token:
        all_headers['Authorization'] = 'Bearer ' + token
    if isinstance(body, dict):
        body = json.dumps(body).encode()
        all_headers['Content-Type'] = 'application/json'
    all_headers.update(headers or {})
    connection.request(method, path, body=body, headers=all_headers)
    response = connection.getresponse()
    data = response.read()
    connection.close()
    if raw:
        return response, data
    return response.status, json.loads(data)


def test_service_lifecycle(service, tmp_path):
    identifier = service.create_from_text(TRANSCRIPT, 'Review', 'P-200')
    record = wait_ready(service, identifier)
    assert record['state'] == 'ready' and record['notes']['sections']['decisions'][0]['id'] == 'D1'
    service.edit(identifier, 'decisions', 'D1', 'Use 6061-T6 at 3.2 mm (edited)')
    assert service.snapshot(identifier)['notes']['sections']['decisions'][0]['edited'] is True
    service.approve(identifier)
    assert service.snapshot(identifier)['review']['approved_at']
    service.edit(identifier, 'lessons', 'L1', delete=True)
    record = service.snapshot(identifier)
    assert record['review']['approved_at'] is None and record['notes']['sections']['lessons'] == []
    assert 'APPROVED' not in service.export_markdown(identifier)
    reloaded = NotesService(tmp_path / 'data', client_factory=FakeClient, start_worker=False)
    assert reloaded.snapshot(identifier)['notes']['sections']['decisions'][0]['text'].endswith('(edited)')
    service.delete(identifier)
    with pytest.raises(KeyError):
        service.snapshot(identifier)


def test_failure_and_retry(tmp_path):
    client = FakeClient(payload='not json')
    service = NotesService(tmp_path / 'd', client_factory=lambda: client)
    identifier = service.create_from_text(TRANSCRIPT)
    record = wait_ready(service, identifier)
    assert record['state'] == 'failed' and 'malformed' in record['error']
    client.payload = __import__('tests.conftest', fromlist=['sample_payload']).sample_payload()
    service.retry(identifier)
    assert wait_ready(service, identifier)['state'] == 'ready'


def test_interrupted_processing_marked_failed(tmp_path):
    service = NotesService(tmp_path / 'd', client_factory=FakeClient, start_worker=False)
    identifier = service.create_from_text(TRANSCRIPT)
    reloaded = NotesService(tmp_path / 'd', client_factory=FakeClient, start_worker=False)
    assert reloaded.snapshot(identifier)['state'] == 'failed'


def test_key_required_without_client(tmp_path, monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    service = NotesService(tmp_path / 'd', start_worker=False)
    with pytest.raises(ValueError, match='API key'):
        service.create_from_text(TRANSCRIPT)


def test_http_auth_host_and_assets(server):
    response, body = request(server, 'GET', '/', token=None, raw=True)
    assert response.status == 200 and b'Engineering' in body
    assert "default-src 'self'" in response.getheader('Content-Security-Policy')
    assert request(server, 'GET', '/api/notes', token=None)[0] == 401
    assert request(server, 'GET', '/api/notes', token='wrong')[0] == 401
    assert request(server, 'GET', '/api/notes', headers={'Host': 'evil.example'})[0] == 403
    assert request(server, 'POST', '/api/paste', {'text': TRANSCRIPT}, headers={'Origin': 'http://evil.example'})[0] == 403


def test_http_paste_review_export(server, service):
    status, body = request(server, 'POST', '/api/paste', {'text': TRANSCRIPT, 'title': 'Bracket review'})
    assert status == 202
    identifier = body['id']
    wait_ready(service, identifier)
    status, record = request(server, 'GET', f'/api/notes/{identifier}')
    assert status == 200 and record['state'] == 'ready'
    status, record = request(server, 'POST', f'/api/notes/{identifier}/edit', {'section': 'decisions', 'item_id': 'D1', 'text': 'New'})
    assert status == 200 and record['notes']['sections']['decisions'][0]['text'] == 'New'
    status, record = request(server, 'POST', f'/api/notes/{identifier}/approve', {'approved': True})
    assert record['review']['approved_at']
    response, data = request(server, 'GET', f'/api/notes/{identifier}/export.md', raw=True)
    assert response.status == 200 and 'Bracket_review.md' in response.getheader('Content-Disposition')
    assert '## B. Engineering Decisions' in data.decode()
    response, data = request(server, 'GET', f'/api/notes/{identifier}/export.pdf', raw=True)
    assert response.status == 200 and data.startswith(b'%PDF')
    assert request(server, 'POST', f'/api/notes/{identifier}/edit', {'section': 'nope', 'item_id': 'D1', 'text': 'x'})[0] == 400
    assert request(server, 'POST', f'/api/notes/{identifier}/delete', {})[0] == 200
    assert request(server, 'GET', f'/api/notes/{identifier}')[0] == 404
    assert request(server, 'GET', '/api/notes/../../etc')[0] in (400, 404)


def test_http_transcript_upload_and_errors(server, service):
    headers = {'X-Filename': 'review%20notes.srt', 'Content-Type': 'application/octet-stream'}
    srt = b'1\n00:00:01,000 --> 00:00:04,000\nAlice: Today we review the pump bracket for the P-200 mount.\n'
    status, body = request(server, 'POST', '/api/upload', srt, headers)
    assert status == 202
    record = wait_ready(service, body['id'])
    assert record['title'] == 'review notes' and record['source']['kind'] == 'transcript'
    assert request(server, 'POST', '/api/upload', b'x', {'X-Filename': 'model.step'})[0] == 400
    assert request(server, 'POST', '/api/upload', b'', {'X-Filename': 'a.txt'})[0] == 400
    assert request(server, 'POST', '/api/paste', {'text': '   '})[0] == 400
    assert request(server, 'POST', '/api/paste', b'{bad', {'Content-Type': 'application/json'})[0] == 400


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='ffmpeg not installed')
def test_http_recording_upload(server, service, tmp_path):
    source = tmp_path / 'meeting.m4a'
    subprocess.run(['ffmpeg', '-nostdin', '-loglevel', 'error', '-f', 'lavfi', '-i', 'sine=duration=3', '-c:a', 'aac',
                    str(source)], check=True)
    status, body = request(server, 'POST', '/api/upload', source.read_bytes(), {'X-Filename': 'meeting.m4a', 'X-Glossary': 'P-200'})
    assert status == 202
    record = wait_ready(service, body['id'])
    assert record['state'] == 'ready', record['error']
    assert record['source']['kind'] == 'recording' and record['transcript']['format'] == 'recording'
    assert record['transcript']['segments'][0]['text'] == 'We will use the 3.2 mm aluminium bracket.'
    assert not (service.directory(body['id']) / 'audio').exists()


def test_concise_bullets_edit_delete_and_follow_item_deletion(service):
    identifier = service.create_from_text(TRANSCRIPT, 'Review', 'P-200')
    record = wait_ready(service, identifier)
    concise = record['notes']['summary']
    assert concise['topics'][0]['title'] == 'Bracket material' and record['notes']['summary_error'] is None
    service.edit(identifier, 'summary', 'N1', 'Aluminium 6061-T6, 3.2 mm thick')
    n1 = lambda: next(b for b in service.snapshot(identifier)['notes']['summary']['topics'][0]['bullets'] if b['id'] == 'N1')
    bullet = n1()
    assert bullet['edited'] is True and bullet['text'].startswith('Aluminium')
    service.edit(identifier, 'open_issues', 'O1', delete=True)
    assert service.snapshot(identifier)['notes']['summary']['next_steps'] == []
    service.edit(identifier, 'summary', 'N2', delete=True)
    assert n1()['children'] == []
    with pytest.raises(ValueError):
        service.edit(identifier, 'summary', 'N99', 'x')
