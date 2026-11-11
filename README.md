# Engineering Design Notes

Upload a recording of an engineering meeting and get structured, source-referenced design notes that you can
review, edit, approve and export as Markdown or PDF.

This is a standalone local app. It doesn't listen to or record meetings. You give it a recording (or an existing
transcript) after the meeting.

```
recording (.m4a/.mp4/...) ──ffmpeg──▶ 16 kHz mono chunks ──Whisper──▶ timestamped transcript segments (S001…)
transcript (.txt/.md/.docx/.srt/.vtt or pasted) ──parser──────────────▶ transcript segments (S001…)
segments ──GPT-4o (strict JSON schema)──▶ deterministic verification ──▶ notes A–G ──GPT-4o──▶ concise topic notes ──▶ review / edit / approve ──▶ Markdown / PDF
video frames ──OpenCV──▶ screenshots + cursor track + pointing moments ──GPT-4.1 vision──▶ a screenshot per note item
```

The notes have seven sections: **A** Engineering Context, **B** Decisions, **C** Design Alternatives,
**D** Rationale and Trade-offs, **E** Supporting Evidence, **F** Open Issues and Follow-up Actions,
**G** Lessons Learned. Every item cites transcript segments (`[S014 · 00:03:15 · Alice]`). Click a citation to see
the transcript excerpt. Sections with nothing supporting them say "Not discussed in transcript".

**Screenshots and cursor position (video uploads).** For `.mp4/.mov/.webm/.mkv` recordings the app also
captures the screen: a screenshot whenever the shared content changes, the cursor position throughout (found in
the frames by its arrow shape and motion, since an uploaded file has no cursor telemetry), and *pointing
moments* where the presenter's cursor came to rest. Each note item is offered only the screenshots and
pointing moments from when it was discussed (cited speech ±3 s). GPT-4.1 vision (`DESIGNNOTES_VISION_MODEL`) keeps a screenshot only if it
shows what that item is about. The item then shows the frame from that moment with the cursor marked (red
ring) and, when the discussed feature is clear, an orange box around it. Answers outside the item's time window
or with low confidence are discarded. You can remove any screenshot. Markdown export becomes a `.zip`
(`notes.md` + `images/`), and the PDF includes the images.

## Requirements

- Python 3.10+
- `ffmpeg` (for recordings only). macOS: `brew install ffmpeg`. Ubuntu: `sudo apt install ffmpeg`.
- An OpenAI API key in a `.env` file in the app folder (`OPENAI_API_KEY=sk-...`). It is read at startup and used for Whisper transcription, GPT-4o extraction and GPT-4.1 vision matching; the UI has no key field.

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # optional: add OPENAI_API_KEY here, or paste it into the app
python -m designnotes
```

The launcher prints a URL like `http://127.0.0.1:53817/#<token>` and opens it in your browser. Options:
`--port 8080`, `--no-browser`, `--data-dir PATH`.

Notes are saved per user in `~/Library/Application Support/Engineering Design Notes` (macOS) or
`~/.local/share/engineering-design-notes` (Linux). Each note folder holds the original upload, the
transcript, the raw model response and your edits. Set `DESIGNNOTES_DATA_DIR` or `--data-dir` to change the
location.

## Use

1. Drop a recording or transcript file, or switch to **Paste transcript**. Supported formats:
   - Recordings: `.mp3 .m4a .wav .mp4 .mov .webm .mkv .ogg .flac .aac .mpeg .mpga`, up to 2 GB.
   - Transcripts: `.txt .md .docx .srt .vtt`, up to 20 MB / 250,000 characters.
2. Optional: add a title, project/component, and a **technical glossary** (part numbers, material grades,
   names). The glossary is passed to Whisper so it spells those terms correctly.
3. Click **Generate Engineering Notes**. Recordings go through Converting → Transcribing → (video only:
   Finding screenshots and cursor movements) → Extracting → (video only: Matching screenshots).
   A 1-hour meeting typically takes a few minutes.
4. Review the notes. **Notes** shows concise, Granola-style topics: short bullets rewritten from the
   verified items, each with a time chip that opens the supporting transcript and the screenshot of that
   moment. **Detailed record** shows every verified item in sections A–G with its source quote. Items
   marked **needs verification** failed an automatic check (a concise bullet is flagged if it states a
   number its items don't support). Edit or delete any bullet or item, then **Approve**. Deleting an item
   also removes it from the concise bullets built on it. Editing an approved note returns it to draft.
5. Export **Markdown** or **PDF**. Both lead with the concise notes and screenshots, followed by the detailed
   record, the review status and a transcript appendix.

## How accuracy is enforced

The model is told to extract only what the transcript states, keep values and identifiers exactly as
spoken, keep proposals out of decisions, and mark interpretations. Its output is constrained to a strict JSON
schema. Every item then goes through deterministic checks before it's shown
(`designnotes/extraction.py`):

| Check | Result if it fails |
|---|---|
| Cited segment IDs exist | Unknown IDs removed; item dropped if none remain and the quote can't be located |
| Supporting quote appears verbatim in the cited segments | Sources corrected if the quote is elsewhere; otherwise flagged and marked *interpretation* |
| Every number in the item appears in its sources (adjacent segments are searched) | Flagged "Value(s) not found in the cited source" |
| Decisions have explicit agreement/commitment wording | Flagged "Confirm this was actually decided" |
| Alternatives marked *selected* have explicit selection wording | Flagged |
| Owners/due dates are stated (or volunteered: "I'll…") | Removed, with a note |
| Participants appear in the transcript; roles are stated | Dropped / role removed |

Dropped items are listed at the end of the notes, so nothing is discarded silently.

Coverage: if a stretch of the meeting (15 s or more of speech) is cited by no item, a second call extracts items for
just those segments, for example a walkthrough of a drawing under **Design presented**. Anything still uncovered is
listed as a warning and at the end of the exports.

## Limitations

- **No speaker identification for recordings.** Whisper doesn't separate speakers, so recording-based
  notes have timestamps but no speaker names. Uploaded transcripts keep their speaker labels.
- **Transcription errors carry over.** Numbers, units and jargon can be misheard, and the value check only
  compares the notes against the transcript, not the audio. The glossary helps. Verify critical values
  against the recording.
- The checks are heuristics. A flagged item may be correct and an unflagged item may still be wrong.
  The notes are drafts until an engineer approves them.
- Recordings and transcripts are sent to OpenAI for processing. Don't upload content your organization
  doesn't allow to be sent to that API.
- English is assumed for transcription (`language='en'`).
- **Cursor detection works from pixels.** Standard arrow pointers are tracked by shape. Other pointer shapes
  (hand, I-beam) are tracked by motion only and are less reliable. If the recording software hides the cursor,
  screenshots are still matched but carry no cursor position. Screenshot matching sends the screenshots and
  cursor close-ups to OpenAI (roughly cents per meeting). If it fails, the notes are still produced and
  **Retry** re-runs only the screenshot steps.
- Single-user local app: no accounts or sharing. Very long meetings (over ~4 hours of speech) need to be split.

## Development

```bash
pip install -r requirements-dev.txt
pytest                      # OpenAI is mocked; ffmpeg tests are skipped if ffmpeg is missing
python scripts/evaluate.py  # real run on examples/transcripts (needs OPENAI_API_KEY); writes evaluation/results/
python scripts/evaluate.py --recording meeting.m4a   # also evaluate a real recording
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for the module layout and data model.
