"""Visual evidence from video recordings: on-screen views, the cursor track and pointing moments.

An uploaded recording carries no cursor telemetry, so everything is recovered from the pixels:

1. ffmpeg decodes grayscale frames at SAMPLE_FPS (downscaled to at most ANALYSIS_WIDTH).
2. Views: a frame whose coarse fingerprint differs from the current view, once the screen has settled,
   becomes a new view (a screenshot), or re-selects an earlier identical view. A fallback observation
   is logged every FALLBACK_S seconds.
3. Cursor: small regions that change between consecutive frames are cursor candidates. Each is scored
   against arrow-pointer templates (both polarities, several sizes); when no candidate looks like an
   arrow (e.g. a hand or I-beam pointer), the candidate that is not the previous cursor location wins.
   While the screen is static the cursor is re-checked at its last position so a hidden cursor is not
   reported.
4. Pointing moments: places where the cursor comes to rest for at least DWELL_S seconds. Presenters
   point at the feature they are talking about, so these are the strongest link between a frame
   region and the speech at that moment.

All positions are normalized to [0, 1] with a top-left origin, relative to the full video frame.
"""
import json
import re
import subprocess
from pathlib import Path

from designnotes.media import ffmpeg_path

SAMPLE_FPS = 4
ANALYSIS_WIDTH = 2000
IMAGE_WIDTH = 1600
CROP_FRACTION = 0.28
FALLBACK_S = 30.0
DWELL_S = 0.75
MAX_VIEWS = 24
MAX_POINTERS = 60
MAX_TRACK = 6000
CHANGE_BITS = 24        # of 256 fingerprint bits: a different view
SAME_VIEW_BITS = 12     # an earlier view shown again
SETTLE_BITS = 4         # consecutive frames this close: the screen has settled
SCENE_CHANGE_FRACTION = 0.02
SHAPE_SCORE = 0.55
HIDDEN_CHECK_S = 2.0
MAX_DURATION_S = 4 * 3600

ARROW = [(0, 0), (0, 17), (4, 13), (7, 20), (10, 19), (7, 12), (12, 12)]
ARROW_HEIGHT = 20


class VisualError(ValueError):
    """Visual analysis failed; the message is safe to show to the user."""


def probe(source):
    """Return {'width', 'height', 'duration_s'} of the first real video stream, or None if there is none."""
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        return None
    result = subprocess.run([ffmpeg, '-nostdin', '-hide_banner', '-i', str(source)],
                            capture_output=True, text=True, timeout=120)
    info = result.stderr or ''
    duration = None
    match = re.search(r'Duration: (\d+):(\d+):(\d+(?:\.\d+)?)', info)
    if match:
        duration = int(match[1]) * 3600 + int(match[2]) * 60 + float(match[3])
    for line in info.splitlines():
        if 'Video:' not in line or 'attached pic' in line:
            continue
        size = re.search(r'[ ,](\d{2,5})x(\d{2,5})[ ,\]]', line)
        if size:
            return {'width': int(size[1]), 'height': int(size[2]), 'duration_s': duration}
    return None


def analysis_size(width, height):
    scaled = min(width, ANALYSIS_WIDTH)
    scaled -= scaled % 2
    other = max(2, int(round(height * scaled / width / 2)) * 2)
    return scaled, other


# ---------------------------------------------------------------- fingerprints

def fingerprint(gray):
    import cv2
    small = cv2.resize(gray, (16, 16), interpolation=cv2.INTER_AREA)
    return small > small.mean()


def distance(first, second):
    return int((first != second).sum())


# ---------------------------------------------------------------- cursor templates

def arrow_templates(heights):
    """Arrow pointer templates: (image, mask, height, tip_offset) for dark and light pointers."""
    import cv2
    import numpy as np
    templates = []
    for height in heights:
        factor = height / ARROW_HEIGHT
        outline = max(1, int(round(factor)))
        size = (int(ARROW_HEIGHT * factor) + 2 * outline + 3, int(13 * factor) + 2 * outline + 3)
        supersample = 4
        points = np.array([[(x * factor + outline + 1) * supersample, (y * factor + outline + 1) * supersample]
                           for x, y in ARROW], dtype=np.int32)
        big = (size[0] * supersample, size[1] * supersample)
        shape = np.zeros(big, np.uint8)
        cv2.fillPoly(shape, [points], 255)
        border = cv2.dilate(shape, np.ones((2 * outline * supersample + 1,) * 2, np.uint8))
        shape = cv2.resize(shape, (size[1], size[0]), interpolation=cv2.INTER_AREA)
        border = cv2.resize(border, (size[1], size[0]), interpolation=cv2.INTER_AREA)
        mask = (border > 64).astype(np.uint8)
        for fill, edge in ((0, 255), (255, 0)):
            image = np.full(size, edge, np.uint8)
            image[shape > 128] = fill
            templates.append((image, mask, height, (outline + 1, outline + 1)))
    return templates


def shape_score(gray, box, templates):
    """Best arrow-template match inside box (x, y, w, h). Returns (score, tip_x, tip_y, height)."""
    import cv2
    import numpy as np
    x, y, w, h = box
    best = (-1.0, None, None, None)
    for image, mask, height, (tip_x, tip_y) in templates:
        th, tw = image.shape
        pad = 4
        x0, y0 = max(0, x - tw // 2 - pad), max(0, y - th // 2 - pad)
        x1, y1 = min(gray.shape[1], x + w + tw // 2 + pad), min(gray.shape[0], y + h + th // 2 + pad)
        window = gray[y0:y1, x0:x1]
        if window.shape[0] < th or window.shape[1] < tw:
            continue
        result = cv2.matchTemplate(window, image, cv2.TM_CCOEFF_NORMED, mask=mask)
        result = np.nan_to_num(result, nan=-1.0, posinf=-1.0, neginf=-1.0)
        _, score, _, location = cv2.minMaxLoc(result)
        if score > best[0]:
            best = (float(score), x0 + location[0] + tip_x, y0 + location[1] + tip_y, height)
    return best


class CursorTracker:
    def __init__(self, width, height):
        self.width, self.height = width, height
        base = height / 1000
        heights = sorted({int(round(base * value)) for value in (11, 13, 15, 17, 20, 24, 28, 34, 40)} - {0})
        self.all_templates = arrow_templates([h for h in heights if h >= 9])
        self.templates = self.all_templates
        self.heights = []
        self.position = None     # (x, y) in analysis pixels
        self.by_shape = False
        self.samples = []        # [t, x_norm, y_norm, method]
        self.last_check = 0.0

    def lock_size(self):
        if len(self.heights) >= 8 and self.templates is self.all_templates:
            common = max(set(self.heights), key=self.heights.count)
            self.templates = [t for t in self.all_templates if 0.75 * common <= t[2] <= 1.3 * common]

    def record(self, t, x, y, method):
        self.position = (x, y)
        self.by_shape = method == 'shape'
        self.samples.append([round(t, 2), round(x / self.width, 4), round(y / self.height, 4), method])

    def hide(self, t):
        if self.position is not None:
            self.samples.append([round(t, 2), None, None, 'hidden'])
        self.position = None

    def update(self, t, gray, previous):
        import cv2
        import numpy as np
        if previous is None:
            return 'start'
        difference = cv2.absdiff(gray, previous)
        mask = (difference > 32).astype(np.uint8)
        changed = float(mask.mean())
        if changed > SCENE_CHANGE_FRACTION:
            return 'scene'
        if changed == 0.0:
            self.check_static(t, gray)
            return 'static'
        mask = cv2.dilate(mask, np.ones((3, 3), np.uint8))
        count, _, stats, _ = cv2.connectedComponentsWithStats(mask)
        limit = max(self.height * 0.09, 24)
        boxes = [tuple(int(v) for v in stats[k][:4]) for k in range(1, count)
                 if stats[k][4] >= 12 and 4 <= stats[k][2] <= limit and 6 <= stats[k][3] <= limit]
        if not boxes or len(boxes) > 12:
            self.check_static(t, gray)
            return 'noise'
        scored = [(shape_score(gray, box, self.templates), box) for box in boxes]
        (score, x, y, height), box = max(scored, key=lambda item: item[0][0])
        if score >= SHAPE_SCORE:
            self.heights.append(height)
            self.lock_size()
            self.record(t, x, y, 'shape')
            return 'moved'
        if self.position is not None and len(boxes) == 2:
            near = [b for b in boxes if b[0] - 8 <= self.position[0] <= b[0] + b[2] + 8
                    and b[1] - 8 <= self.position[1] <= b[1] + b[3] + 8]
            if len(near) == 1:
                other = boxes[1] if near[0] is boxes[0] else boxes[0]
                self.record(t, other[0], other[1], 'motion')
                return 'moved'
        return 'unknown'

    def check_static(self, t, gray):
        if self.position is None or not self.by_shape or t - self.last_check < HIDDEN_CHECK_S:
            return
        self.last_check = t
        x, y = self.position
        score = shape_score(gray, (int(x), int(y), 4, 4), self.templates)[0]
        if score < SHAPE_SCORE * 0.8:
            self.hide(t)


# ---------------------------------------------------------------- views

class ViewSelector:
    def __init__(self):
        self.views = []          # {'id', 'timestamp_s', 'hash', 'observations'}
        self.timeline = []       # [t, view_id] whenever the displayed view changes
        self.current = None
        self.previous_hash = None
        self.last_observation = -1e9

    def observe(self, view, t, reason):
        if view['observations'] and view['observations'][-1]['timestamp_s'] == t:
            return
        if len(view['observations']) < 200:
            view['observations'].append({'timestamp_s': t, 'reason': reason})
        self.last_observation = t
        if self.current is not view:
            self.timeline.append([t, view['id']])
        self.current = view

    def update(self, t, gray):
        hash_value = fingerprint(gray)
        settled = self.previous_hash is not None and distance(hash_value, self.previous_hash) <= SETTLE_BITS
        self.previous_hash = hash_value
        if self.current is None:
            self.select(t, hash_value, 'start')
            return
        if settled and distance(hash_value, self.current['hash']) >= CHANGE_BITS:
            self.select(t, hash_value, 'change')
        elif t - self.last_observation >= FALLBACK_S:
            self.observe(self.current, t, 'fallback')

    def select(self, t, hash_value, reason):
        view = min(self.views, key=lambda v: distance(v['hash'], hash_value), default=None)
        if view is None or distance(view['hash'], hash_value) > SAME_VIEW_BITS:
            if len(self.views) >= MAX_VIEWS:
                return
            view = {'id': f'V{len(self.views) + 1:02d}', 'timestamp_s': t, 'hash': hash_value, 'observations': []}
            self.views.append(view)
        self.observe(view, t, reason)


def view_at(timeline, t):
    current = None
    for start, identifier in timeline:
        if start > t + 1e-6:
            break
        current = identifier
    return current


def cursor_at(track, t, max_age=None):
    """Last known cursor position at time t (normalized), or None if unknown or hidden."""
    position = None
    for sample in track:
        if sample[0] > t + 1e-6:
            break
        if max_age is not None and t - sample[0] > max_age and sample[3] == 'motion':
            position = None
            continue
        position = None if sample[1] is None else [sample[1], sample[2]]
    return position


def pointing_moments(track, timeline, duration, radius):
    """Places the cursor rested for at least DWELL_S seconds: [{'id','start_s','end_s','x','y','view_id'}]."""
    clusters = []
    for t, x, y, method in track:
        if x is None:
            if clusters:
                clusters[-1]['end'] = t
            clusters.append(None)
            continue
        last = clusters[-1] if clusters else None
        if last is not None and (x - last['x']) ** 2 + (y - last['y']) ** 2 <= radius ** 2:
            last.update(x=x, y=y, settled=t)
        else:
            if last is not None:
                last['end'] = t
            clusters.append({'start': t, 'settled': t, 'end': None, 'x': x, 'y': y})
    moments = []
    for cluster in clusters:
        if cluster is None:
            continue
        end = cluster['end'] if cluster['end'] is not None else duration
        if end - cluster['settled'] >= DWELL_S:
            moments.append({'start_s': round(cluster['settled'], 2), 'end_s': round(end, 2),
                            'x': cluster['x'], 'y': cluster['y'],
                            'view_id': view_at(timeline, cluster['settled'])})
    if len(moments) > MAX_POINTERS:
        keep = sorted(moments, key=lambda m: m['end_s'] - m['start_s'], reverse=True)[:MAX_POINTERS]
        moments = sorted(keep, key=lambda m: m['start_s'])
    for number, moment in enumerate(moments, start=1):
        moment['id'] = f'P{number:03d}'
    return moments


# ---------------------------------------------------------------- frame extraction

def extract_frame(source, t, target, width=IMAGE_WIDTH, crop=None):
    """Save the color frame at t seconds as JPEG. crop = (x, y, w, h) normalized."""
    ffmpeg = ffmpeg_path()
    filters = []
    if crop:
        x, y, w, h = crop
        filters.append(f'crop=iw*{w:.4f}:ih*{h:.4f}:iw*{x:.4f}:ih*{y:.4f}')
    filters.append(f"scale='min({width},iw)':-2")
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-noautorotate',
               '-ss', f'{max(0.0, t):.3f}', '-i', str(source), '-frames:v', '1', '-vf', ','.join(filters),
               '-q:v', '3', str(target)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    if result.returncode != 0 or not target.exists():
        raise VisualError('A frame could not be extracted from the recording.')
    return target


def pointer_crop(moment, aspect):
    w = CROP_FRACTION
    h = min(1.0, CROP_FRACTION * aspect)
    x = min(max(0.0, moment['x'] - w / 2), 1 - w)
    y = min(max(0.0, moment['y'] - h / 2), 1 - h)
    return x, y, w, h


# ---------------------------------------------------------------- analysis

def analyze(source, directory, progress=None):
    """Analyze a video recording. Saves views/ and pointers/ JPEGs under directory; returns the analysis dict."""
    import numpy as np
    info = probe(source)
    if info is None:
        return None
    width, height = analysis_size(info['width'], info['height'])
    directory = Path(directory)
    ffmpeg = ffmpeg_path()
    command = [ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error', '-noautorotate', '-i', str(source),
               '-an', '-vf', f'fps={SAMPLE_FPS},scale={width}:{height}', '-pix_fmt', 'gray', '-f', 'rawvideo', '-']
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    tracker, selector = CursorTracker(width, height), ViewSelector()
    frame_bytes = width * height
    previous, index, t = None, 0, 0.0
    try:
        while True:
            data = process.stdout.read(frame_bytes)
            if len(data) < frame_bytes:
                break
            t = index / SAMPLE_FPS
            if t > MAX_DURATION_S:
                break
            gray = np.frombuffer(data, np.uint8).reshape(height, width)
            selector.update(t, gray)
            tracker.update(t, gray, previous)
            previous = gray
            index += 1
            if progress and index % (SAMPLE_FPS * 60) == 0:
                progress(f'Finding screenshots and cursor movements ({int(t // 60)} min analyzed)')
    finally:
        process.stdout.close()
        process.kill()
        process.wait()
    if index == 0:
        raise VisualError('No video frames could be decoded from the recording.')
    duration = info['duration_s'] or t
    track = tracker.samples
    radius = 0.012 * max(1.0, width / height)
    moments = pointing_moments(track, selector.timeline, duration, radius)
    if len(track) > MAX_TRACK:
        step = len(track) / MAX_TRACK
        track = [track[int(i * step)] for i in range(MAX_TRACK)]

    views = []
    for number, view in enumerate(selector.views, start=1):
        if progress:
            progress(f'Saving screenshots ({number} of {len(selector.views)})')
        moment = min(view['timestamp_s'] + 1.0 / SAMPLE_FPS, max(0.0, duration - 0.05))
        extract_frame(source, moment, directory / 'views' / f'{view["id"]}.jpg')
        views.append({'id': view['id'], 'timestamp_s': view['timestamp_s'], 'observations': view['observations']})
    aspect = info['width'] / info['height']
    for moment in moments:
        moment['crop'] = [round(v, 4) for v in pointer_crop(moment, aspect)]
        extract_frame(source, max(moment['start_s'], moment['end_s'] - 0.25),
                      directory / 'pointers' / f'{moment["id"]}.jpg', width=512, crop=moment['crop'])
    shape_samples = sum(1 for sample in tracker.samples if sample[3] == 'shape')
    return {'width': info['width'], 'height': info['height'], 'duration_s': round(duration, 2),
            'sample_fps': SAMPLE_FPS, 'views': views, 'timeline': selector.timeline, 'pointers': moments,
            'track': track, 'cursor': {'samples': len(tracker.samples), 'shape_samples': shape_samples,
                                        'detected': len(tracker.samples) > 0}}


VISUAL_FILE = re.compile(r'[A-Z]\d{1,3}\.jpg')


def annotate(path, visual):
    """JPEG bytes of a visual with the cursor (red ring) and highlighted region (orange box) drawn in."""
    import io
    from PIL import Image, ImageDraw
    image = Image.open(path).convert('RGB')
    width, height = image.size
    draw = ImageDraw.Draw(image)
    scale = max(width, height) / 1000
    if visual.get('bbox'):
        x0, y0, x1, y1 = visual['bbox']
        draw.rectangle([x0 * width, y0 * height, x1 * width, y1 * height], outline=(240, 140, 0),
                       width=max(2, int(3 * scale)))
    if visual.get('cursor_norm'):
        x, y = visual['cursor_norm'][0] * width, visual['cursor_norm'][1] * height
        radius = 16 * scale
        draw.ellipse([x - radius, y - radius, x + radius, y + radius], outline=(255, 255, 255), width=max(3, int(7 * scale)))
        draw.ellipse([x - radius, y - radius, x + radius, y + radius], outline=(224, 49, 43), width=max(2, int(4 * scale)))
    output = io.BytesIO()
    image.save(output, 'JPEG', quality=85)
    return output.getvalue()


def caption(visual):
    from designnotes.transcript import format_time
    parts = [f'Screenshot at {format_time(visual["timestamp_s"])}']
    if visual.get('cursor_source') == 'pointing':
        parts.append('presenter\'s cursor resting here (red ring)')
    elif visual.get('cursor_norm'):
        parts.append('cursor position (red ring)')
    else:
        parts.append('cursor not visible')
    if visual.get('bbox'):
        parts.append('discussed feature boxed in orange')
    parts.append(f'{visual["confidence"]} confidence')
    text = ', '.join(parts)
    return text + (f'. {visual["rationale"]}' if visual.get('rationale') else '')


if __name__ == '__main__':
    import sys
    result = analyze(sys.argv[1], Path(sys.argv[2]), print)
    summary = {k: v for k, v in result.items() if k not in ('track',)}
    print(json.dumps(summary, indent=1)[:6000])
