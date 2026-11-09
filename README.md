# Engineering Design Notes

Upload a recording of an engineering meeting and get structured, source-referenced design notes that you can
review, edit, approve and export as Markdown or PDF.

This is a standalone local app. It doesn't listen to or record meetings. You give it a recording (or an existing
transcript) after the meeting.

```
recording (.m4a/.mp4/...) ──ffmpeg──▶ 16 kHz mono chunks ──Whisper──▶ timestamped transcript segments (S001…)
transcript (.txt/.md/.docx/.srt/.vtt or pasted) ──parser──────────────▶ transcript segments (S001…)
segments ──GPT-4o (strict JSON schema)──▶ deterministic verification ──▶ notes A–G ──▶ review / edit / approve ──▶ Markdown / PDF
```

The notes have seven sections: **A** Engineering Context, **B** Decisions, **C** Design Alternatives,
**D** Rationale and Trade-offs, **E** Supporting Evidence, **F** Open Issues and Follow-up Actions,
**G** Lessons Learned. Every item cites transcript segments (`[S014 · 00:03:15 · Alice]`). Click a citation to see
the transcript excerpt. Sections with nothing supporting them say "Not discussed in transcript".

## Requirements

- Python 3.10+
- `ffmpeg` (for recordings only). macOS: `brew install ffmpeg`. Ubuntu: `sudo apt install ffmpeg`.
- An OpenAI API key. It's used for Whisper transcription and GPT-4o extraction.

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
3. Click **Generate Engineering Notes**. Recordings go through Converting → Transcribing → Extracting.
   A 1-hour meeting typically takes a few minutes.
4. Review the notes. Items marked **needs verification** failed an automatic check. Edit or delete any
   item, click a source tag to read the excerpt, then **Approve**. Editing an approved note returns it to draft.
5. Export **Markdown** or **PDF**. Both include the review status and a full transcript appendix.

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
- Single-user local app: no accounts or sharing. Very long meetings (over ~4 hours of speech) need to be split.

## Development

```bash
pip install -r requirements-dev.txt
pytest                      # OpenAI is mocked; ffmpeg tests are skipped if ffmpeg is missing
python scripts/evaluate.py  # real run on examples/transcripts (needs OPENAI_API_KEY); writes evaluation/results/
python scripts/evaluate.py --recording meeting.m4a   # also evaluate a real recording
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for the module layout and data model.
