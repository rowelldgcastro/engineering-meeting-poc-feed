# Architecture

A single-process local app: a Python standard-library HTTP server bound to `127.0.0.1`, a static
HTML/JS frontend, a background worker thread, and JSON files on disk. The only third-party dependencies
are `openai`, `fpdf2` and `python-dotenv`.

## Modules (`designnotes/`)

| Module | Responsibility | Depends on |
|---|---|---|
| `transcript.py` | Parse pasted/uploaded transcripts (txt, md, docx, srt, vtt) into segments; decoding; limits | stdlib |
| `media.py` | `ffmpeg` conversion to 16 kHz mono MP3 in 10-minute chunks (each under the 25 MB API limit); Whisper transcription; chunk offsets mapped back to recording time | `transcript.finalize` |
| `visuals.py` | Video analysis (OpenCV on ffmpeg-decoded frames at 4 fps): view (screenshot) selection by fingerprint change, cursor tracking by arrow-template matching and motion, pointing moments (cursor at rest ≥ 0.75 s), frame extraction, export annotation | `media.ffmpeg_path` |
| `grounding.py` | Per-item candidate screenshots/pointing moments from the cited speech window (±3 s), GPT-4o vision call with strict schema, validation of answers against candidates, frame capture at the chosen moment | `visuals` |
| `extraction.py` | Prompt, strict JSON schema, model call, deterministic verification (`build_notes`) | none (client passed in) |
| `report.py` | Markdown rendering | `transcript.source_label` |
| `pdf.py` | PDF rendering (`fpdf2`), same content as Markdown | `report` helpers |
| `service.py` | Notes records, persistence, upload validation, job queue, review/edit/approve/export | all of the above |
| `server.py` | HTTP routes, auth, host/origin checks, launcher | `service` |
| `static/` | UI: upload/paste, progress, notes review, source panel | HTTP API |

Extraction, note generation and export are separate modules with no UI or HTTP code, so they can be
reused from scripts (`scripts/evaluate.py` does this).

## Data model

Transcript segment (same shape for recordings and transcripts):

```json
{"id": "S014", "start_s": 195.0, "end_s": 203.5, "speaker": "Alice", "text": "…", "line": 31}
```

`speaker` is `null` for recordings. `line` is the source line number for text transcripts and `null` for
recordings.

Notes item (e.g. a decision):

```json
{"id": "D1", "text": "Use 6061-T6 aluminium at 3.2 mm", "quote": "let's go with the aluminium at 3.2 mm",
 "source_ids": ["S002", "S003"], "basis": "stated", "uncertainty": null,
 "parameters": ["6061-T6", "3.2 mm"], "rationale": "Weight matters most.",
 "flags": [{"code": "source_extended", "level": "info", "message": "…"}], "edited": false}
```

Section-specific fields: context `aspect` (subject/objective/background); alternatives `option`,
`advantages`, `disadvantages`, `outcome` (selected/rejected/undecided), `outcome_reason`; rationale `kind`,
`factors`; evidence `kind`; open issues `kind`, `owner`, `due`. `flags` with `level: warning` are shown as
"needs verification".

Record on disk (`<data-dir>/<32-hex id>/record.json`) holds the title, project, glossary, source
metadata, state (`queued → transcribing → analyzing → extracting → matching → ready | failed`), progress text, error,
transcript, `visual` (analysis: `views`, `timeline`, `pointers`, `track`, `cursor`), `visual_status`, notes and
review (`approved_at`, `edited_at`). The folder also keeps `source.<ext>` (the original upload),
`model_response.json` / `grounding_response.json` (raw model output, for auditing), `frames/views/V01.jpg…`,
`frames/pointers/P001.jpg…` (cursor close-ups) and `visuals/<item id>.jpg` (the frame attached to each item).

Visual attached to an item (`item["visuals"]`, at most one). Coordinates are normalized to the full frame, top-left origin:

```json
{"file": "D1.jpg", "view_id": "V01", "timestamp_s": 137.1, "pointer_id": "P010", "cursor_norm": [0.676, 0.572],
 "cursor_source": "pointing", "bbox": [0.52, 0.41, 0.83, 0.70], "confidence": "high", "rationale": "…"}
```

`cursor_source` is `pointing` (cursor at rest on the feature), `tracked` (last known position) or `null`
(not visible). Video analysis and matching failures never fail the notes; they set `visual_status.state =
failed`, and Retry re-runs only those steps. Temporary audio chunks are
deleted after transcription. If the app stops mid-job, the record is marked failed on the next start and can
be retried; the upload is kept.

## Security

- Binds to `127.0.0.1` only. Every API call needs a per-launch bearer token, passed to the browser in the
  URL fragment. `Host`/`Origin` must be loopback, which blocks DNS rebinding and cross-site requests.
- Strict CSP (no inline script). All rendering uses `textContent`, never `innerHTML`.
- An API key entered in the UI is held in memory only. Data folders are created with `0700`.
- Upload names are sanitized, and IDs are validated as 32 hex characters before any filesystem access.

## Testing

`tests/` uses a fake OpenAI client (`tests/conftest.py`). Coverage includes parser formats and edge cases,
ffmpeg chunking with generated audio/video, extraction verification rules, Markdown/PDF rendering,
service lifecycle and persistence, and HTTP routes, auth and uploads. `tests/test_visuals.py` renders a
synthetic screen recording with a moving arrow cursor and two slides, and checks view selection, cursor
tracking, pointing moments, candidate windows, rejection of out-of-window/low-confidence answers, exports,
screenshot removal and the image route. `scripts/evaluate.py` runs the
real models on `examples/transcripts/` and scores them against `examples/expectations.json` (expected
facts, decision counts, and proposals that must not appear as decisions).
