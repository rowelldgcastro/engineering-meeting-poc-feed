import json
import subprocess
import zipfile
import io

import numpy as np
import cv2
import pytest

from designnotes import grounding, visuals
from designnotes.pdf import render_pdf
from designnotes.report import render_markdown
from designnotes.service import NotesService
from tests.conftest import FakeClient

pytestmark = pytest.mark.skipif(not visuals.ffmpeg_path(), reason='ffmpeg required')

WIDTH, HEIGHT, FPS = 640, 480, 10
# (start_s, end_s, from_xy, to_xy): cursor moves linearly, otherwise rests at the last position
PATH = [(0, 2, (100, 100), (100, 100)), (2, 3, (100, 100), (400, 300)), (3, 6, (400, 300), (400, 300)),
        (6.5, 7.5, (400, 300), (200, 350)), (7.5, 12, (200, 350), (200, 350))]


def slide(number):
    image = np.full((HEIGHT, WIDTH), 245, np.uint8)
    if number == 1:
        cv2.rectangle(image, (300, 220), (520, 380), 40, 3)
        cv2.putText(image, 'L=100mm', (320, 200), cv2.FONT_HERSHEY_SIMPLEX, 1, 30, 2)
    else:
        cv2.circle(image, (220, 330), 70, 40, 3)
        cv2.putText(image, 'L=75mm', (120, 220), cv2.FONT_HERSHEY_SIMPLEX, 1, 30, 2)
    return image


def cursor_position(t):
    for start, end, first, last in PATH:
        if start <= t < end:
            f = (t - start) / (end - start)
            return first[0] + (last[0] - first[0]) * f, first[1] + (last[1] - first[1]) * f
    for start, end, first, last in reversed(PATH):
        if t >= end:
            return last
    return PATH[0][2]


def draw_cursor(image, x, y, height=16):
    factor = height / visuals.ARROW_HEIGHT
    points = np.array([[x + px * factor, y + py * factor] for px, py in visuals.ARROW], np.int32)
    cv2.polylines(image, [points], True, 255, 3)
    cv2.fillPoly(image, [points], 0)


@pytest.fixture(scope='module')
def video(tmp_path_factory):
    path = tmp_path_factory.mktemp('video') / 'meeting.mp4'
    process = subprocess.Popen(
        ['ffmpeg', '-nostdin', '-loglevel', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'gray', '-s', f'{WIDTH}x{HEIGHT}',
         '-r', str(FPS), '-i', '-', '-f', 'lavfi', '-i', 'sine=duration=12', '-shortest', '-c:v', 'libx264', '-crf', '16',
         '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(path)], stdin=subprocess.PIPE)
    for index in range(12 * FPS):
        t = index / FPS
        frame = slide(1 if t < 6.25 else 2)
        draw_cursor(frame, *cursor_position(t))
        process.stdin.write(frame.tobytes())
    process.stdin.close()
    assert process.wait() == 0
    return path


@pytest.fixture(scope='module')
def analysis(video, tmp_path_factory):
    directory = tmp_path_factory.mktemp('frames')
    return visuals.analyze(video, directory), directory


def test_probe_detects_video_and_rejects_audio(video, tmp_path):
    assert visuals.probe(video) == {'width': WIDTH, 'height': HEIGHT, 'duration_s': pytest.approx(12, abs=0.2)}
    audio = tmp_path / 'a.m4a'
    subprocess.run(['ffmpeg', '-nostdin', '-loglevel', 'error', '-f', 'lavfi', '-i', 'sine=duration=1', str(audio)], check=True)
    assert visuals.probe(audio) is None
    assert visuals.analyze(audio, tmp_path / 'frames') is None


def test_views_follow_screen_changes(analysis):
    result, directory = analysis
    assert [v['id'] for v in result['views']] == ['V01', 'V02']
    assert [view for _, view in result['timeline']] == ['V01', 'V02']
    assert 6.0 <= result['timeline'][1][0] <= 7.0
    assert (directory / 'views' / 'V01.jpg').is_file() and (directory / 'views' / 'V02.jpg').is_file()


def test_cursor_track_and_pointing_moments(analysis):
    result, directory = analysis
    assert result['cursor']['detected'] and result['cursor']['shape_samples'] > 0
    near = lambda p, x, y: abs(p['x'] - x / WIDTH) < 0.02 and abs(p['y'] - y / HEIGHT) < 0.02
    first = [p for p in result['pointers'] if near(p, 400, 300)]
    second = [p for p in result['pointers'] if near(p, 200, 350)]
    assert first and first[0]['view_id'] == 'V01' and first[0]['start_s'] <= 3.5 and first[0]['end_s'] >= 6.0
    assert second and second[0]['view_id'] == 'V02' and second[0]['end_s'] >= 11.5
    assert (directory / 'pointers' / f'{first[0]["id"]}.jpg').is_file()
    position = visuals.cursor_at(result['track'], 10.0)
    assert position == pytest.approx([200 / WIDTH, 350 / HEIGHT], abs=0.02)


def test_pointing_moments_and_cursor_lookup_units():
    track = [[1.0, 0.1, 0.1, 'shape'], [1.25, 0.5, 0.5, 'shape'], [1.5, 0.501, 0.5, 'shape'],
             [4.0, 0.8, 0.2, 'shape'], [4.2, None, None, 'hidden']]
    moments = visuals.pointing_moments(track, [[0.0, 'V01']], 10.0, 0.01)
    assert [(m['id'], m['x'], m['start_s'], m['end_s']) for m in moments] == [('P001', 0.501, 1.5, 4.0)]
    assert visuals.cursor_at(track, 2.0) == [0.501, 0.5]
    assert visuals.cursor_at(track, 5.0) is None
    assert visuals.cursor_at(track, 0.5) is None


def segments():
    return [{'id': 'S001', 'start_s': 1.0, 'end_s': 2.5, 'speaker': None, 'text': 'This box is L=100mm today.'},
            {'id': 'S002', 'start_s': 8.0, 'end_s': 10.0, 'speaker': None, 'text': 'The new one is 75 mm.'}]


def grounding_answer(view, pointer, confidence='high', bbox=None, cells=None):
    def answer(request):
        text = request['messages'][1]['content'][0]['text']
        items = json.loads(text.split('\n', 1)[1])
        return {'results': [{'item_id': i['item_id'], 'view_id': view(i), 'pointer_id': pointer(i),
                             'cells': cells, 'bbox': bbox, 'confidence': confidence,
                             'rationale': 'Shows the bracket.'} for i in items]}
    return answer


def test_candidates_are_limited_to_speech_window(analysis):
    result, _ = analysis
    by_id = {s['id']: s for s in segments()}
    found = grounding.item_candidates({'source_ids': ['S001']}, by_id, result)
    assert found['views'] == ['V01'] and all(p['view_id'] == 'V01' for p in found['pointers'])
    later = grounding.item_candidates({'source_ids': ['S002']}, by_id, result)
    assert 'V02' in later['views']
    assert grounding.item_candidates({'source_ids': ['S999']}, by_id, result) is None


def test_attach_validates_model_answers(analysis, video, tmp_path):
    result, frames = analysis
    directory = tmp_path / 'record'
    (directory / 'frames').mkdir(parents=True)
    for sub in ('views', 'pointers'):
        (directory / 'frames' / sub).symlink_to(frames / sub)
    pointers = {p['view_id']: p['id'] for p in result['pointers']}
    notes = {'sections': {
        'decisions': [{'id': 'D1', 'text': 'Box stays L=100mm', 'source_ids': ['S001']},
                      {'id': 'D2', 'text': 'New length 75 mm', 'source_ids': ['S002']}],
        'evidence': [{'id': 'E1', 'text': 'Untimed', 'source_ids': []}],
        'participants': [{'id': 'P1', 'name': 'Alice', 'source_ids': ['S001']}]}}
    # D2 answers with a view that was not on screen for D1's window: must be rejected for D1 but fine for D2.
    client = FakeClient(grounding=grounding_answer(lambda i: 'V02' if i['item_id'] == 'D1' else i['candidate_views'][-1],
                                                   lambda i: pointers.get('V02'), bbox=[0.1, 0.4, 0.5, 0.9]))
    summary, raw, _ = grounding.attach(client, notes, {'segments': segments()}, result, video, directory)
    d1, d2 = notes['sections']['decisions']
    assert d1['visuals'] == [] and notes['sections']['evidence'][0]['visuals'] == []
    visual = d2['visuals'][0]
    assert visual['view_id'] == 'V02' and visual['pointer_id'] == pointers['V02'] and visual['cursor_source'] == 'pointing'
    assert visual['cursor_norm'] == pytest.approx([200 / WIDTH, 350 / HEIGHT], abs=0.02)
    assert visual['bbox'] == [0.1, 0.4, 0.5, 0.9]
    assert (directory / 'visuals' / 'D2.jpg').is_file()
    assert summary['matched'] == 1 and summary['with_cursor'] == 1
    request = client.requests[0]
    images = [part for part in request['messages'][1]['content'] if part['type'] == 'image_url']
    assert images and all(part['image_url']['url'].startswith('data:image/jpeg;base64,') for part in images)
    assert 'visuals' not in notes['sections']['participants'][0]


def test_low_confidence_and_unknown_ids_are_dropped(analysis, video, tmp_path):
    result, frames = analysis
    directory = tmp_path / 'record'
    (directory / 'frames').mkdir(parents=True)
    for sub in ('views', 'pointers'):
        (directory / 'frames' / sub).symlink_to(frames / sub)
    notes = {'sections': {'decisions': [{'id': 'D1', 'text': 'x', 'source_ids': ['S001']}]}}
    for client in (FakeClient(grounding=grounding_answer(lambda i: 'V01', lambda i: None, confidence='low')),
                   FakeClient(grounding=grounding_answer(lambda i: 'V09', lambda i: 'P999'))):
        summary, _, _ = grounding.attach(client, notes, {'segments': segments()}, result, video, directory)
        assert notes['sections']['decisions'][0]['visuals'] == [] and summary['matched'] == 0


def test_clean_bbox():
    assert grounding.clean_bbox([0.1, 0.2, 0.3, 0.4]) == [0.1, 0.2, 0.3, 0.4]
    assert grounding.clean_bbox([0.1, 0.2, 0.1, 0.4]) is None
    assert grounding.clean_bbox([0, 0, 1, 1]) is None
    assert grounding.clean_bbox(None) is None and grounding.clean_bbox(['a', 1, 2, 3]) is None


def wait_ready(service, identifier):
    import time
    deadline = time.time() + 60
    while time.time() < deadline:
        record = service.snapshot(identifier)
        if record['state'] in ('ready', 'failed'):
            return record
        time.sleep(0.05)
    raise AssertionError('timed out')


def video_client(**extra):
    timed = [{'start': 3.0, 'end': 5.0, 'text': ' The pump bracket for the P-200 mount is the box.'},
             {'start': 8.0, 'end': 10.0, 'text': " Agreed, let's go with the aluminium at 3.2 mm."}]
    payload = {key: [] for key in ('context', 'participants', 'decisions', 'alternatives', 'rationale', 'evidence',
                                   'open_issues', 'lessons')}
    payload['decisions'] = [{'text': 'Use aluminium at 3.2 mm', 'quote': "let's go with the aluminium at 3.2 mm",
                             'source_ids': ['S002'], 'basis': 'stated', 'uncertainty': None,
                             'parameters': ['3.2 mm'], 'rationale': None}]
    answer = grounding_answer(lambda i: i['candidate_views'][-1],
                              lambda i: (i['candidate_pointers'] or [{}])[-1].get('pointer_id'))
    return lambda: FakeClient(payload=payload, segments=timed, grounding=answer, **extra)


def test_service_video_pipeline_exports_and_removal(video, tmp_path):
    service = NotesService(tmp_path / 'data', client_factory=video_client(), start_worker=True)
    with video.open('rb') as stream:
        identifier = service.create_from_upload('meeting.mp4', stream, video.stat().st_size)
    record = wait_ready(service, identifier)
    assert record['state'] == 'ready', record['error']
    assert record['visual_status']['state'] == 'matched' and record['visual_status']['matched'] == 1
    visual = record['notes']['sections']['decisions'][0]['visuals'][0]
    assert visual['view_id'] == 'V02' and visual['cursor_norm'] is not None
    assert service.visual_path(identifier, visual['file']).is_file()
    with pytest.raises(ValueError):
        service.visual_path(identifier, '../record.json')
    with pytest.raises(KeyError):
        service.visual_path(identifier, 'Z9.jpg')

    data, extension = service.export_bundle(identifier)
    assert extension == 'zip'
    archive = zipfile.ZipFile(io.BytesIO(data))
    markdown = archive.read('notes.md').decode()
    assert '![D1 screenshot](images/D1.jpg)' in markdown and 'red ring' in markdown
    assert archive.read('images/D1.jpg')[:2] == b'\xff\xd8'
    assert service.export_pdf(identifier)[:4] == b'%PDF'
    plain = render_markdown(service.snapshot(identifier))
    assert 'Screenshot at 00:00:' in plain and '![' not in plain

    service.edit(identifier, 'decisions', 'D1', remove_visual=True)
    record = service.snapshot(identifier)
    assert record['notes']['sections']['decisions'][0]['visuals'] == []
    assert service.export_bundle(identifier)[1] == 'md'
    with pytest.raises(ValueError):
        service.edit(identifier, 'decisions', 'D1', remove_visual=True)


def test_grounding_failure_keeps_notes_and_allows_retry(video, tmp_path):
    factory = video_client()
    calls = []

    def flaky():
        client = factory()
        answer = client.grounding

        def grounding_once_failing(request):
            calls.append(request)
            if len(calls) == 1:
                raise RuntimeError('network down')
            return answer(request)
        client.grounding = grounding_once_failing
        return client
    service = NotesService(tmp_path / 'data', client_factory=flaky)
    with video.open('rb') as stream:
        identifier = service.create_from_upload('meeting.mp4', stream, video.stat().st_size)
    record = wait_ready(service, identifier)
    assert record['state'] == 'ready' and record['visual_status']['state'] == 'failed'
    assert 'visuals' not in record['notes']['sections']['decisions'][0]
    service.retry(identifier)
    record = wait_ready(service, identifier)
    assert record['visual_status']['state'] == 'matched'


def test_annotate_and_caption(tmp_path):
    from PIL import Image
    path = tmp_path / 'x.jpg'
    Image.new('RGB', (200, 100), 'white').save(path)
    visual = {'timestamp_s': 65, 'cursor_norm': [0.5, 0.5], 'cursor_source': 'pointing', 'bbox': [0.1, 0.1, 0.4, 0.6],
              'confidence': 'high', 'rationale': 'Shows L.'}
    image = Image.open(io.BytesIO(visuals.annotate(path, visual)))
    assert image.size == (200, 100) and image.getpixel((100, 100 // 2 - 16))[0] > 150
    assert visuals.caption(visual).startswith('Screenshot at 00:01:05, presenter')


def test_http_visual_route_and_zip_export(video, tmp_path):
    import threading
    from http.server import ThreadingHTTPServer
    from designnotes.server import handler_factory
    from tests.test_service_server import request
    service = NotesService(tmp_path / 'data', client_factory=video_client())
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), handler_factory(service, 'test-token'))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        status, body = request(httpd, 'POST', '/api/upload', video.read_bytes(), {'X-Filename': 'meeting.mp4'})
        assert status == 202
        record = wait_ready(service, body['id'])
        name = record['notes']['sections']['decisions'][0]['visuals'][0]['file']
        response, data = request(httpd, 'GET', f'/api/notes/{body["id"]}/visuals/{name}', raw=True)
        assert response.status == 200 and response.getheader('Content-Type') == 'image/jpeg' and data[:2] == b'\xff\xd8'
        response, _ = request(httpd, 'GET', f'/api/notes/{body["id"]}/visuals/{name}', token=None, raw=True)
        assert response.status == 401
        response, _ = request(httpd, 'GET', f'/api/notes/{body["id"]}/visuals/record.json', raw=True)
        assert response.status == 400
        response, data = request(httpd, 'GET', f'/api/notes/{body["id"]}/export.md', raw=True)
        assert response.getheader('Content-Type') == 'application/zip' and '.zip' in response.getheader('Content-Disposition')
        assert 'blob:' in response.getheader('Content-Security-Policy')
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_grid_cells_mapped_to_frame_and_must_be_specific_and_at_cursor():
    pointer = {'x': 0.5, 'y': 0.5, 'crop': [0.36, 0.3, 0.28, 0.4]}
    assert grounding.cells_to_frame(['c3', 'D3'], pointer) == [0.472, 0.46, 0.584, 0.54]
    assert grounding.cells_to_frame(['A1'], pointer) is None
    assert grounding.cells_to_frame([f'{c}{r}' for c in 'ABCDE' for r in range(1, 6)], pointer) is None
    assert grounding.cells_to_frame(['Z9', 'x'], pointer) is None and grounding.cells_to_frame(None, pointer) is None
    found = {'views': ['V01'], 'pointers': [dict(pointer, id='P001', view_id='V01', start_s=1.0, end_s=3.0)],
             'speech_start': 2.0, 'start': 0.0, 'end': 5.0}
    result = {'view_id': 'V01', 'pointer_id': 'P001', 'cells': ['C3'], 'bbox': [0.1, 0.1, 0.2, 0.2],
              'confidence': 'high', 'rationale': 'x'}
    visual = {'timeline': [[0.0, 'V01']], 'track': []}
    assert grounding.resolve(result, found, visual)['bbox'] == [0.472, 0.46, 0.528, 0.54]
    result['cells'] = None
    assert grounding.resolve(result, found, visual)['bbox'] == [0.1, 0.1, 0.2, 0.2]
    result['bbox'] = [0.1, 0.1, 0.9, 0.9]
    assert grounding.resolve(result, found, visual)['bbox'] is None


def test_gridded_closeup_is_a_jpeg(tmp_path):
    from PIL import Image
    Image.new('RGB', (512, 365), 'white').save(tmp_path / 'p.jpg')
    assert grounding.gridded_url(tmp_path / 'p.jpg').startswith('data:image/jpeg;base64,/9j/')
