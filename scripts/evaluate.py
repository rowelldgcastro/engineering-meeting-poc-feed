"""Run the extraction pipeline on the synthetic transcripts and write outputs plus a scorecard.

Usage:  OPENAI_API_KEY=... python scripts/evaluate.py [--recording path/to/meeting.m4a]
Results go to evaluation/results/<timestamp>/.
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from designnotes import extraction, media  # noqa: E402
from designnotes.report import render_markdown  # noqa: E402
from designnotes.transcript import parse_transcript  # noqa: E402

EXAMPLES = ROOT / 'examples' / 'transcripts'


def section_text(notes, section):
    items = notes['sections'].get(section, [])
    return [' '.join(extraction.item_strings(section, item) + [item.get('quote', '')]).lower() for item in items]


def score(notes, expectation):
    checks = []
    decisions = notes['sections']['decisions']
    low, high = expectation['decisions']['min'], expectation['decisions']['max']
    checks.append((f'{low}-{high} decisions (got {len(decisions)})', low <= len(decisions) <= high))
    for rule in expectation['expected']:
        texts = section_text(notes, rule['section'])
        if 'all' in rule:
            ok = any(all(term.lower() in text for term in rule['all']) for text in texts)
        else:
            ok = any(term.lower() in text for text in texts for term in rule['any'])
        checks.append((f"{rule['section']}: {rule['note']}", ok))
    decision_text = ' '.join(section_text(notes, 'decisions'))
    for phrase in expectation['forbidden_decisions']:
        checks.append((f'not a decision: "{phrase}"', phrase.lower() not in decision_text))
    sourced = all(item['source_ids'] for items in notes['sections'].values() for item in items)
    checks.append(('every item has a source reference', sourced))
    return checks


def record_for(title, transcript, notes, kind, filename):
    return {'id': '0' * 32, 'title': title, 'project': None, 'created_at': time.time(), 'state': 'ready',
            'source': {'kind': kind, 'filename': filename}, 'transcript': transcript, 'notes': notes,
            'review': {'approved_at': None, 'edited_at': None}}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--recording', action='append', default=[], help='Also process a meeting recording (repeatable)')
    parser.add_argument('--model', default=None)
    args = parser.parse_args()
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / '.env')
    except ImportError:
        pass
    from openai import OpenAI
    client = OpenAI(timeout=600, max_retries=2)
    expectations = json.loads((ROOT / 'examples' / 'expectations.json').read_text())
    out = ROOT / 'evaluation' / 'results' / time.strftime('%Y%m%d-%H%M%S')
    out.mkdir(parents=True)
    lines = ['# Evaluation scorecard', '', f'Model: {args.model or extraction.MODEL}', '']
    total = passed = 0
    for path in sorted(EXAMPLES.iterdir()):
        transcript = parse_transcript(path.read_text(encoding='utf-8'))
        started = time.time()
        notes, raw, usage = extraction.extract(client, transcript, path.stem, model=args.model)
        elapsed = time.time() - started
        (out / f'{path.stem}.raw.json').write_text(raw, encoding='utf-8')
        (out / f'{path.stem}.notes.json').write_text(json.dumps(notes, indent=2), encoding='utf-8')
        (out / f'{path.stem}.md').write_text(render_markdown(record_for(path.stem, transcript, notes, 'transcript', path.name)))
        checks = score(notes, expectations[path.name])
        total += len(checks)
        passed += sum(ok for _, ok in checks)
        lines += [f'## {path.name}', '', f"Scenario: {expectations[path.name]['scenario']}  ",
                  f"Items: {notes['stats']['items']}, flagged: {notes['stats']['flagged']}, dropped: {notes['stats']['dropped']}, "
                  f'time: {elapsed:.1f}s', '']
        lines += [f"- [{'x' if ok else ' '}] {label}" for label, ok in checks] + ['']
        print(f"{path.name}: {sum(ok for _, ok in checks)}/{len(checks)}")
    for recording in args.recording:
        recording = Path(recording)
        started = time.time()
        transcript = media.transcribe_recording(client, recording, out / f'{recording.stem}_audio')
        notes, raw, usage = extraction.extract(client, transcript, recording.stem, model=args.model)
        (out / f'{recording.stem}.transcript.json').write_text(json.dumps(transcript, indent=2), encoding='utf-8')
        (out / f'{recording.stem}.md').write_text(render_markdown(record_for(recording.stem, transcript, notes, 'recording', recording.name)))
        lines += [f'## Recording: {recording.name}', '', f"{len(transcript['segments'])} segments, "
                  f"{notes['stats']['items']} items, {notes['stats']['flagged']} flagged, {time.time() - started:.1f}s", '']
        print(f'{recording.name}: {len(transcript["segments"])} segments, {notes["stats"]["items"]} items')
    lines.insert(4, f'**Automatic checks passed: {passed}/{total}**\n')
    (out / 'scorecard.md').write_text('\n'.join(lines), encoding='utf-8')
    print(f'Passed {passed}/{total}. Results: {out}')


if __name__ == '__main__':
    main()
