"""Note generation: render verified extraction records as readable engineering design notes (Markdown)."""
from datetime import datetime, timezone

from designnotes.extraction import SECTIONS
from designnotes.transcript import format_time, source_label

NOT_DISCUSSED = 'Not discussed in transcript.'
CONTEXT_HEADINGS = [('subject', 'Subject / component'), ('objective', 'Engineering problem / design objective'),
                    ('background', 'Relevant technical background')]
LABELS = {'trade_off': 'Trade-off', 'rationale': 'Rationale', 'assumption': 'Assumption', 'constraint': 'Constraint',
          'calculation': 'Calculation', 'value': 'Value', 'test_result': 'Test result', 'simulation': 'Simulation',
          'document': 'Referenced document', 'observation': 'Observation', 'question': 'Open question',
          'investigation': 'Investigation requested', 'action': 'Follow-up action',
          'selected': 'Selected', 'rejected': 'Rejected', 'undecided': 'Undecided'}


def references(item, segments_by_id):
    labels = [source_label(segments_by_id[ref]) for ref in item.get('source_ids', []) if ref in segments_by_id]
    return ' '.join(f'[{label}]' for label in labels)


def item_detail_lines(section, item):
    """Structured detail lines for an item (shared by Markdown and PDF renderers)."""
    lines = []
    if section == 'decisions':
        if item.get('parameters'):
            lines.append(('Parameters', '; '.join(item['parameters'])))
        lines.append(('Reason', item.get('rationale') or 'No reason stated in transcript.'))
    elif section == 'alternatives':
        lines.append(('Outcome', LABELS[item['outcome']] + (f': {item["outcome_reason"]}' if item.get('outcome_reason') else '')))
        if item.get('advantages'):
            lines.append(('Advantages', '; '.join(item['advantages'])))
        if item.get('disadvantages'):
            lines.append(('Disadvantages', '; '.join(item['disadvantages'])))
    elif section == 'rationale' and item.get('factors'):
        lines.append(('Factors', ', '.join(item['factors'])))
    elif section == 'open_issues':
        lines.append(('Owner', item.get('owner') or 'Not stated'))
        if item.get('due'):
            lines.append(('Due', item['due']))
    if item.get('quote'):
        lines.append(('Source quote', f'"{item["quote"]}"'))
    if item.get('uncertainty'):
        lines.append(('Uncertainty', item['uncertainty']))
    for flag in item.get('flags', []):
        if flag['level'] == 'warning':
            lines.append(('Verification', flag['message']))
    return lines


def item_title(section, item):
    prefix = ''
    if section == 'alternatives':
        prefix = f'{item["option"]} - '
    elif section in ('rationale', 'evidence', 'open_issues'):
        prefix = f'{LABELS[item["kind"]]}: '
    markers = []
    if item.get('basis') == 'interpreted':
        markers.append('interpretation')
    if item.get('edited'):
        markers.append('edited by reviewer')
    if any(flag['level'] == 'warning' for flag in item.get('flags', [])):
        markers.append('needs verification')
    suffix = f' _({", ".join(markers)})_' if markers else ''
    return prefix + item['text'], suffix


def render_markdown(record):
    notes, transcript = record['notes'], record['transcript']
    segments_by_id = {s['id']: s for s in transcript['segments']}
    sections = notes['sections']
    out = [f'# {notes["title"]}', '']
    meta = []
    if notes.get('project'):
        meta.append(f'**Project / component:** {notes["project"]}')
    source = record.get('source', {})
    source_text = source.get('filename') or 'Pasted transcript'
    duration = max((s['end_s'] or s['start_s'] or 0) for s in transcript['segments'])
    source_text += f' ({transcript["format"]}, {len(transcript["segments"])} segments'
    source_text += f', {format_time(duration)})' if transcript.get('has_timestamps') else ')'
    meta.append(f'**Source:** {source_text}')
    created = datetime.fromtimestamp(record['created_at'], timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    meta.append(f'**Generated:** {created} by {notes.get("model", "LLM")} extraction with automatic source verification')
    review = record.get('review', {})
    if review.get('approved_at'):
        approved = datetime.fromtimestamp(review['approved_at'], timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        meta.append(f'**Status:** Approved by reviewer ({approved})')
    else:
        meta.append('**Status:** Draft - not yet reviewed')
    out.append('  \n'.join(meta))
    out += ['', '> Generated from the meeting transcript. Source tags such as `[S014 · 00:03:15 · Alice]` point to '
                'transcript segments listed in the appendix. Items marked _interpretation_ are model readings rather '
                'than explicit statements; items marked _needs verification_ failed an automatic check.', '']

    for key, letter, title, _ in SECTIONS:
        out.append(f'## {letter}. {title}')
        out.append('')
        items = sections.get(key, [])
        if key == 'context':
            for aspect, heading in CONTEXT_HEADINGS:
                out.append(f'### {heading}')
                aspect_items = [item for item in items if item['aspect'] == aspect]
                out += [render_item(key, item, segments_by_id) for item in aspect_items] or [NOT_DISCUSSED]
                out.append('')
            out.append('### Participants')
            participants = sections.get('participants', [])
            if participants:
                for person in participants:
                    role = f' - {person["role"]}' if person.get('role') else ''
                    out.append(f'- {person["name"]}{role} {references(person, segments_by_id)}')
            else:
                out.append('Not identifiable from transcript.')
            out.append('')
            continue
        if not items:
            out += [NOT_DISCUSSED, '']
            continue
        out += [render_item(key, item, segments_by_id) for item in items]
        out.append('')

    cited = list(dict.fromkeys(ref for items in sections.values() for item in items for ref in item.get('source_ids', [])))
    cited = [ref for ref in sorted(cited, key=lambda r: list(segments_by_id).index(r)) if ref in segments_by_id]
    out += ['## Appendix: Cited transcript segments', '']
    out += [f'- **[{source_label(segments_by_id[ref])}]** {segments_by_id[ref]["text"]}' for ref in cited] or ['None.']
    out.append('')
    return '\n'.join(out)


def render_item(section, item, segments_by_id):
    title, suffix = item_title(section, item)
    lines = [f'- **{title}**{suffix} {references(item, segments_by_id)}'.rstrip()]
    for label, value in item_detail_lines(section, item):
        lines.append(f'  - {label}: {value}')
    return '\n'.join(lines)
