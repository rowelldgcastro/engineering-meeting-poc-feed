"""Visual grounding: link each note item to the screenshot, and the cursor pointing moment, that support it.

Candidates are deterministic: only screenshots on screen while the item's cited speech was spoken (plus
WINDOW_S seconds either side) and pointing moments inside that window are offered. A vision model then
decides, per item, whether a candidate actually shows what the item is about. Answers that reference
anything outside the item's candidates, or that are low confidence, are discarded.
"""
import base64
import json
import os
from pathlib import Path

from designnotes import visuals
from designnotes.transcript import format_time

MODEL = os.getenv('DESIGNNOTES_VISION_MODEL', 'gpt-4o')
WINDOW_S = 3.0
MAX_POINTERS_PER_ITEM = 6
MAX_ITEMS = 150
VISUAL_SECTIONS = ('context', 'decisions', 'alternatives', 'rationale', 'evidence', 'open_issues', 'lessons')

GROUNDING_SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': ['results'],
    'properties': {'results': {'type': 'array', 'items': {
        'type': 'object', 'additionalProperties': False,
        'required': ['item_id', 'view_id', 'pointer_id', 'bbox', 'confidence', 'rationale'],
        'properties': {
            'item_id': {'type': 'string'},
            'view_id': {'type': ['string', 'null'], 'description': 'Screenshot that shows what the item is about, or null.'},
            'pointer_id': {'type': ['string', 'null'],
                           'description': 'Pointing moment whose cursor is on the discussed feature, or null.'},
            'bbox': {'anyOf': [{'type': 'array', 'items': {'type': 'number'}}, {'type': 'null'}],
                     'description': '[x0, y0, x1, y1] normalized region of the discussed feature, or null.'},
            'confidence': {'type': 'string', 'enum': ['high', 'medium', 'low']},
            'rationale': {'type': 'string', 'description': 'One sentence: what in the screenshot supports the item.'},
        }}}}}

SYSTEM_PROMPT = '''You link engineering meeting notes to screenshots taken from the meeting recording.

For each note item you receive the transcript segments it cites, the time window in which it was discussed,
the screenshots that were on screen during that window, and pointing moments: times when the presenter's
cursor came to rest on a spot. Pointer positions are normalized (x, y) from the top-left of the full frame,
and each comes with a close-up crop centered on the cursor.

Rules:
- Choose a view_id only if that screenshot visibly shows the specific content the item is about (the drawing,
  part, dimension, value, chart, table or text). A webcam view, title slide or unrelated content is not
  support: use null.
- Choose a pointer_id only if, at that moment, the cursor is on the feature the item discusses. Prefer the
  pointing moment closest to when the item was spoken. A pointer must belong to the chosen screenshot.
- The cursor marks a point, not a region. Give bbox only when the discussed feature is clearly identifiable in
  the screenshot, as [x0, y0, x1, y1] normalized to the full frame; otherwise null.
- confidence: high = the screenshot clearly shows what the item states; medium = it shows the discussed
  subject but not every detail of the claim; low = weak or doubtful.
- Use only the ids offered for that item. Return one result per item.'''


def item_candidates(item, segments_by_id, visual):
    times = []
    for ref in item.get('source_ids', []):
        segment = segments_by_id.get(ref)
        if segment and segment.get('start_s') is not None:
            times.append((segment['start_s'], segment.get('end_s') or segment['start_s']))
    if not times:
        return None
    speech_start, speech_end = min(t[0] for t in times), max(t[1] for t in times)
    start, end = speech_start - WINDOW_S, speech_end + WINDOW_S
    timeline = visual['timeline']
    views = [visuals.view_at(timeline, start)] + [v for t, v in timeline if start < t <= end]
    views = list(dict.fromkeys(v for v in views if v))
    middle = (speech_start + speech_end) / 2
    pointers = [p for p in visual['pointers'] if p['start_s'] <= end and p['end_s'] >= start and p['view_id'] in views]
    pointers = sorted(pointers, key=lambda p: abs(min(max(middle, p['start_s']), p['end_s']) - middle))
    pointers = sorted(pointers[:MAX_POINTERS_PER_ITEM], key=lambda p: p['start_s'])
    if not views:
        return None
    return {'speech_start': speech_start, 'speech_end': speech_end, 'start': start, 'end': end,
            'views': views, 'pointers': pointers}


def view_spans(visual):
    spans = {}
    timeline = visual['timeline']
    for index, (t, view) in enumerate(timeline):
        finish = timeline[index + 1][0] if index + 1 < len(timeline) else visual['duration_s']
        spans.setdefault(view, []).append((t, finish))
    return spans


def data_url(path):
    return 'data:image/jpeg;base64,' + base64.b64encode(Path(path).read_bytes()).decode('ascii')


def build_request(items, segments_by_id, visual, frames):
    spans = view_spans(visual)
    view_ids = list(dict.fromkeys(v for _, _, c in items for v in c['views']))
    pointer_ids = list(dict.fromkeys(p['id'] for _, _, c in items for p in c['pointers']))
    pointers = {p['id']: p for p in visual['pointers']}
    payload = []
    for section, item, found in items:
        cited = [f'{ref} [{format_time(segments_by_id[ref]["start_s"])}] {segments_by_id[ref]["text"]}'
                 for ref in item.get('source_ids', []) if ref in segments_by_id]
        payload.append({
            'item_id': item['id'], 'section': section, 'text': item['text'],
            'option': item.get('option'), 'quote': item.get('quote') or None, 'cited_segments': cited,
            'discussed': f'{format_time(found["speech_start"])}-{format_time(found["speech_end"])}',
            'candidate_views': found['views'],
            'candidate_pointers': [{'pointer_id': p['id'], 'view_id': p['view_id'],
                                    'resting': f'{format_time(p["start_s"])}-{format_time(p["end_s"])}',
                                    'cursor': [p['x'], p['y']]} for p in found['pointers']]})
    content = [{'type': 'text', 'text': 'Note items:\n' + json.dumps(payload, indent=1)}]
    for view in view_ids:
        shown = ', '.join(f'{format_time(a)}-{format_time(b)}' for a, b in spans.get(view, []))
        content.append({'type': 'text', 'text': f'Screenshot {view} (on screen {shown}):'})
        content.append({'type': 'image_url', 'image_url': {'url': data_url(frames / 'views' / f'{view}.jpg'),
                                                           'detail': 'high'}})
    for identifier in pointer_ids:
        pointer = pointers[identifier]
        content.append({'type': 'text', 'text': (
            f'Pointing moment {identifier} on {pointer["view_id"]}, resting {format_time(pointer["start_s"])}-'
            f'{format_time(pointer["end_s"])}, cursor at ({pointer["x"]:.3f}, {pointer["y"]:.3f}); close-up crop '
            f'of region {pointer["crop"]} with the cursor at its center:')})
        content.append({'type': 'image_url', 'image_url': {'url': data_url(frames / 'pointers' / f'{identifier}.jpg'),
                                                           'detail': 'low'}})
    return content


def call_model(client, content, model=None):
    response = client.chat.completions.create(
        model=model or MODEL, temperature=0, max_tokens=12000,
        response_format={'type': 'json_schema',
                         'json_schema': {'name': 'visual_grounding', 'strict': True, 'schema': GROUNDING_SCHEMA}},
        messages=[{'role': 'system', 'content': SYSTEM_PROMPT}, {'role': 'user', 'content': content}])
    choice = response.choices[0]
    if choice.finish_reason != 'stop' or getattr(choice.message, 'refusal', None):
        raise visuals.VisualError('The screenshot matching response was incomplete.')
    usage = response.usage.model_dump() if getattr(response, 'usage', None) is not None and hasattr(response.usage, 'model_dump') else {}
    return choice.message.content or '', usage


def clean_bbox(bbox):
    if not isinstance(bbox, list) or len(bbox) != 4 or not all(isinstance(v, (int, float)) for v in bbox):
        return None
    x0, y0, x1, y1 = [min(1.0, max(0.0, float(v))) for v in bbox]
    if x1 - x0 < 0.01 or y1 - y0 < 0.01 or (x1 - x0) * (y1 - y0) > 0.9:
        return None
    return [round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4)]


def choose_moment(found, view_id, pointer, visual):
    """Timestamp of the frame to show: while the cursor rests on the pointed spot, else when the item is spoken."""
    if pointer:
        early, late = pointer['start_s'] + 0.1, max(pointer['start_s'] + 0.1, pointer['end_s'] - 0.1)
        return min(max(found['speech_start'], early), late)
    if visuals.view_at(visual['timeline'], found['speech_start']) == view_id:
        return found['speech_start']
    for t, view in visual['timeline']:
        if view == view_id and found['start'] <= t <= found['end']:
            return t + 0.5
    return max(found['start'], 0.0)


def resolve(result, found, visual):
    """Validate one model answer against the item's candidates. Returns a visual dict or None."""
    view_id = result.get('view_id')
    if view_id not in found['views'] or result.get('confidence') not in ('high', 'medium'):
        return None
    pointer = next((p for p in found['pointers'] if p['id'] == result.get('pointer_id')), None)
    if pointer is not None and pointer['view_id'] != view_id:
        pointer = None
    moment = round(choose_moment(found, view_id, pointer, visual), 2)
    if pointer:
        cursor, cursor_source = [pointer['x'], pointer['y']], 'pointing'
    else:
        cursor = visuals.cursor_at(visual['track'], moment)
        cursor_source = 'tracked' if cursor else None
    return {'view_id': view_id, 'timestamp_s': moment, 'pointer_id': pointer['id'] if pointer else None,
            'cursor_norm': cursor, 'cursor_source': cursor_source, 'bbox': clean_bbox(result.get('bbox')),
            'confidence': result['confidence'], 'rationale': str(result.get('rationale') or '')[:500]}


def attach(client, notes, transcript, visual, source, directory, model=None):
    """Attach item['visuals'] to note items. Returns (summary, raw_response, usage)."""
    directory = Path(directory)
    frames = directory / 'frames'
    segments_by_id = {s['id']: s for s in transcript['segments']}
    items = []
    for section in VISUAL_SECTIONS:
        for item in notes['sections'].get(section, []):
            item['visuals'] = []
            found = item_candidates(item, segments_by_id, visual)
            if found and len(items) < MAX_ITEMS:
                items.append((section, item, found))
    summary = {'items': sum(len(notes['sections'].get(s, [])) for s in VISUAL_SECTIONS), 'candidates': len(items),
               'matched': 0, 'with_cursor': 0, 'model': model or MODEL}
    if not items:
        return summary, '', {}
    raw, usage = call_model(client, build_request(items, segments_by_id, visual, frames), model)
    try:
        results = {r['item_id']: r for r in json.loads(raw)['results'] if isinstance(r, dict)}
    except (ValueError, KeyError, TypeError):
        raise visuals.VisualError('The screenshot matching response was malformed.') from None
    for section, item, found in items:
        chosen = resolve(results.get(item['id'], {}), found, visual)
        if not chosen:
            continue
        name = f'{item["id"]}.jpg'
        visuals.extract_frame(source, chosen['timestamp_s'], directory / 'visuals' / name)
        item['visuals'] = [dict(chosen, file=name)]
        summary['matched'] += 1
        summary['with_cursor'] += bool(chosen['cursor_norm'])
    return summary, raw, usage
