'use strict';
const $ = id => document.getElementById(id);
const token = location.hash.slice(1) || sessionStorage.getItem('designNotesToken') || '';
if (location.hash) { sessionStorage.setItem('designNotesToken', token); history.replaceState(null, '', '/'); }
let view = 'summary', status = null, selected = null, record = null, mode = 'upload', file = null, lastSignature = '', activeRef = null, poller = null;

function node(tag, text, className) {
  const el = document.createElement(tag);
  if (text !== undefined && text !== null) el.textContent = text;
  if (className) el.className = className;
  return el;
}
function showError(error) { $('error').textContent = error ? String(error.message || error) : ''; }
async function api(path, body, raw) {
  const options = {method: body === undefined ? 'GET' : 'POST', headers: {'Authorization': 'Bearer ' + token}};
  if (body !== undefined) { options.headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(body); }
  const response = await fetch(path, options);
  if (raw && response.ok) return response;
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Request failed.');
  return data;
}
function formatTime(seconds) {
  if (seconds === null || seconds === undefined) return null;
  const s = Math.round(seconds), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
  return [h, m, r].map(v => String(v).padStart(2, '0')).join(':');
}
function sourceLabel(segment) {
  return [segment.id, formatTime(segment.start_s), segment.speaker].filter(Boolean).join(' · ');
}
function action(id, callback) {
  $(id).addEventListener('click', async () => {
    $(id).disabled = true; showError(null);
    try { await callback(); } catch (error) { showError(error); } finally { $(id).disabled = false; }
  });
}

// ---------------------------------------------------------------- status, list
async function refreshStatus() {
  status = await api('/api/status');
  $('keyState').textContent = status.api_key_set ? 'OpenAI key configured' : 'OpenAI key required';
  $('keyBox').classList.toggle('hidden', status.api_key_set);
  $('ffmpegWarning').classList.toggle('hidden', status.ffmpeg);
  $('formats').textContent = 'Recordings: ' + status.media_extensions.join(' ') + '  ·  Transcripts: ' + status.transcript_extensions.join(' ');
}
async function refreshList() {
  const items = await api('/api/notes');
  $('notesList').replaceChildren(...items.map(item => {
    const button = node('button', item.title, item.id === selected ? 'selected' : '');
    const label = item.state === 'ready' ? (item.approved ? 'approved' : 'draft') : item.state;
    button.append(node('span', (item.source_kind === 'recording' ? 'recording' : 'transcript') + ' · ' + label + ' · ' +
      new Date(item.created_at * 1000).toLocaleString(), 'state'));
    button.addEventListener('click', () => openNotes(item.id).catch(showError));
    return button;
  }));
  if (!items.length) $('notesList').append(node('p', 'No notes yet.', 'muted small'));
}

// ---------------------------------------------------------------- input view
function setMode(next) {
  mode = next;
  $('tabUpload').classList.toggle('active', mode === 'upload'); $('tabUpload').setAttribute('aria-selected', mode === 'upload');
  $('tabPaste').classList.toggle('active', mode === 'paste'); $('tabPaste').setAttribute('aria-selected', mode === 'paste');
  $('uploadPane').classList.toggle('hidden', mode !== 'upload');
  $('pastePane').classList.toggle('hidden', mode !== 'paste');
  $('glossaryBox').classList.toggle('hidden', mode !== 'upload');
}
function setFile(chosen) {
  file = chosen || null;
  $('fileName').textContent = file ? file.name + ' (' + (file.size / 1048576).toFixed(1) + ' MB)' : '';
}
function showInput() {
  selected = null; record = null; lastSignature = '';
  $('inputView').classList.remove('hidden'); $('notesView').classList.add('hidden');
  refreshList().catch(showError);
}
async function generate() {
  let result;
  if (mode === 'paste') {
    const text = $('pasteText').value;
    if (!text.trim()) throw new Error('Paste a transcript first.');
    result = await api('/api/paste', {text, title: $('title').value, project: $('project').value});
  } else {
    if (!file) throw new Error('Choose a recording or transcript file first.');
    $('generate').textContent = 'Uploading…';
    try {
      const response = await fetch('/api/upload', {method: 'POST', body: file, headers: {
        'Authorization': 'Bearer ' + token, 'Content-Type': 'application/octet-stream',
        'X-Filename': encodeURIComponent(file.name), 'X-Title': encodeURIComponent($('title').value),
        'X-Project': encodeURIComponent($('project').value), 'X-Glossary': encodeURIComponent($('glossary').value)}});
      result = await response.json();
      if (!response.ok) throw new Error(result.error || 'Upload failed.');
    } finally { $('generate').textContent = 'Generate Engineering Notes'; }
  }
  $('pasteText').value = ''; $('title').value = ''; $('project').value = ''; $('glossary').value = ''; setFile(null); $('file').value = '';
  await openNotes(result.id);
}

// ---------------------------------------------------------------- notes view
async function openNotes(id) {
  selected = id; lastSignature = ''; activeRef = null;
  $('inputView').classList.add('hidden'); $('notesView').classList.remove('hidden');
  $('sourceExcerpt').replaceChildren();
  await refreshNotes();
  await refreshList();
}
async function refreshNotes() {
  if (!selected) return;
  record = await api('/api/notes/' + selected);
  renderRecord();
}
function renderRecord() {
  const r = record, ready = r.state === 'ready';
  $('notesTitle').textContent = r.title;
  const meta = [];
  if (r.project) meta.push('Project/component: ' + r.project);
  meta.push((r.source.filename || 'Pasted transcript') + ' · ' + new Date(r.created_at * 1000).toLocaleString());
  if (r.transcript) meta.push(r.transcript.segments.length + ' transcript segments');
  if (r.visual_status && r.visual_status.message && ready) meta.push(r.visual_status.message);
  $('notesMeta').textContent = meta.join('  ·  ');
  $('progress').textContent = r.state === 'failed' ? 'Failed: ' + (r.error || '') : (r.progress || r.state) + '…';
  $('progress').className = (r.state === 'failed' ? 'warning' : 'progress') + (ready ? ' hidden' : '');
  for (const id of ['approve', 'exportMd', 'exportPdf', 'editTitle']) $(id).classList.toggle('hidden', !ready);
  $('retry').classList.toggle('hidden', !(r.state === 'failed' || (ready && r.visual_status?.state === 'failed')));
  const approved = ready && r.review.approved_at;
  $('approve').textContent = approved ? 'Withdraw approval' : 'Approve notes';
  $('reviewState').replaceChildren();
  if (ready) {
    $('reviewState').append(node('p', approved ? 'Approved by reviewer on ' + new Date(r.review.approved_at * 1000).toLocaleString() + '.'
      : 'Draft: review each item, edit or delete as needed, then approve. Editing after approval returns the notes to draft.',
      approved ? 'approved' : 'draft'));
  }
  const warnings = [...(r.transcript?.warnings || [])];
  if (ready) {
    const stats = r.notes.stats, flagged = countFlagged();
    if (flagged) warnings.push(flagged + ' item(s) failed an automatic check (marked "needs verification"). Check them against the source.');
    const gaps = r.notes.coverage_gaps || [];
    if (gaps.length) warnings.push('No notes cite these parts of the meeting: ' + gaps.map(g => g.start_s == null
      ? g.source_ids[0] + '-' + g.source_ids[g.source_ids.length - 1]
      : formatTime(g.start_s) + '-' + formatTime(g.end_s)).join(', ') + '. Check the transcript there.');
    if (r.notes.summary_error) warnings.push(r.notes.summary_error);
    if (stats.dropped) warnings.push(stats.dropped + ' extracted item(s) were discarded because no supporting transcript text was found.');
  }
  $('warnings').textContent = warnings.join('\n');
  $('warnings').classList.toggle('hidden', !warnings.length);
  const signature = JSON.stringify([r.state, r.notes, r.transcript?.segments.length]);
  if (signature !== lastSignature) {
    lastSignature = signature;
    renderNotes();
    renderTranscript();
  }
}
function countFlagged() {
  return Object.values(record.notes.sections).flat().filter(item => (item.flags || []).some(f => f.level === 'warning')).length;
}
const LABELS = {trade_off: 'Trade-off', rationale: 'Rationale', assumption: 'Assumption', constraint: 'Constraint',
  calculation: 'Calculation', value: 'Value', test_result: 'Test result', simulation: 'Simulation', document: 'Document',
  observation: 'Observation', question: 'Open question', investigation: 'Investigation', action: 'Action',
  selected: 'Selected', rejected: 'Rejected', undecided: 'Undecided', subject: 'Subject / component',
  objective: 'Problem / objective', design: 'Design presented', background: 'Technical background'};

function renderNotes() {
  const container = $('notes');
  container.replaceChildren();
  if (record.state !== 'ready') {
    container.append(node('div', record.state === 'failed' ? 'No notes were generated.' : 'Notes will appear here when processing finishes.', 'card muted'));
    return;
  }
  const concise = record.notes.summary;
  $('viewTabs').classList.toggle('hidden', !concise);
  if (concise && view === 'summary') { container.append(renderSummary(concise)); return; }
  const sections = record.notes.sections;
  status.sections.forEach(section => {
    const card = node('section', undefined, 'card section');
    card.append(node('h2', section.letter + '. ' + section.title), node('p', section.description, 'muted small desc'));
    if (section.key === 'context') {
      ['subject', 'objective', 'design', 'background'].forEach(aspect => {
        card.append(node('h3', LABELS[aspect]));
        const items = sections.context.filter(item => item.aspect === aspect);
        items.forEach(item => card.append(renderItem('context', item)));
        if (!items.length) card.append(node('p', 'Not discussed in transcript.', 'empty'));
      });
      card.append(node('h3', 'Participants'));
      sections.participants.forEach(item => card.append(renderItem('participants', item)));
      if (!sections.participants.length) card.append(node('p', 'Not identifiable from transcript.', 'empty'));
    } else {
      const items = sections[section.key] || [];
      items.forEach(item => card.append(renderItem(section.key, item)));
      if (!items.length) card.append(node('p', 'Not discussed in transcript.', 'empty'));
    }
    container.append(card);
  });
  if (record.notes.dropped.length) {
    const card = node('details', undefined, 'card dropped');
    card.append(node('summary', 'Discarded by verification (' + record.notes.dropped.length + ')'));
    record.notes.dropped.forEach(d => card.append(node('p', d.section + ': ' + (d.text || '') + ' — ' + d.reason, 'muted')));
    container.append(card);
  }
}
function setView(next) {
  view = next;
  [['viewSummary', 'summary'], ['viewDetailed', 'detailed']].forEach(([id, value]) => {
    $(id).classList.toggle('active', view === value); $(id).setAttribute('aria-selected', String(view === value));
  });
  if (record && record.state === 'ready') renderNotes();
}
function bulletVisuals(concise) {
  // Same rule as summary.bullet_visuals: one screenshot per top-level bullet, each screen moment once.
  const items = Object.fromEntries(Object.values(record.notes.sections).flat().map(item => [item.id, item]));
  const moment = v => v.pointer_id || v.view_id + '@' + Math.round(v.timestamp_s || 0);
  const score = v => [!!v.bbox, v.cursor_source === 'pointing', v.confidence === 'high'].map(Number).join('');
  const shown = new Set(), chosen = {};
  const tops = concise.topics.flatMap(topic => topic.bullets).concat(concise.next_steps || []);
  for (const bullet of tops) {
    if (bullet.visual_hidden) continue;
    const ids = bullet.item_ids.concat(...(bullet.children || []).map(child => child.item_ids));
    const options = ids.flatMap(id => (items[id] && items[id].visuals) || []).filter(v => !shown.has(moment(v)));
    if (!options.length) continue;
    const best = options.reduce((a, b) => score(b) > score(a) ? b : a);
    shown.add(moment(best)); chosen[bullet.id] = best;
  }
  return chosen;
}
function timeSpan(bullet) {
  const segments = Object.fromEntries(record.transcript.segments.map(s => [s.id, s]));
  const cited = (bullet.source_ids || []).map(id => segments[id]).filter(Boolean);
  if (!cited.length) return '';
  const starts = cited.map(s => s.start_s).filter(v => v !== null && v !== undefined);
  if (!starts.length) return cited.length > 1 ? cited[0].id + '–' + cited[cited.length - 1].id : cited[0].id;
  const first = formatTime(Math.min(...starts)), last = formatTime(Math.max(...starts));
  return first === last ? first : first + '–' + last;
}
function renderSummary(concise) {
  const card = node('section', undefined, 'card concise');
  const visuals = bulletVisuals(concise);
  const list = bullets => { const ul = node('ul'); bullets.forEach(b => ul.append(renderBullet(b, visuals))); return ul; };
  concise.topics.forEach(topic => { card.append(node('h2', topic.title), list(topic.bullets)); });
  if ((concise.next_steps || []).length) card.append(node('h2', 'Next steps'), list(concise.next_steps));
  card.append(node('p', 'Times open the supporting transcript. Every bullet is built from verified items in the detailed record.', 'muted small'));
  return card;
}
function renderBullet(bullet, visuals) {
  const li = node('li', undefined, 'bullet' + ((bullet.flags || []).some(f => f.level === 'warning') ? ' flagged' : ''));
  li.dataset.item = bullet.id;
  const row = node('div', undefined, 'bullet-row');
  const body = node('span', bullet.text, 'bullet-text');
  row.append(body);
  const span = timeSpan(bullet);
  if (span) {
    const chip = node('button', span, 'ref time');
    chip.type = 'button'; chip.title = 'Show transcript excerpt';
    chip.addEventListener('click', () => showSource(bullet, bullet.source_ids[0]));
    row.append(chip);
  }
  if (bullet.edited) row.append(node('span', 'edited', 'badge edited'));
  (bullet.flags || []).filter(f => f.level === 'warning').forEach(flag => {
    const badge = node('span', 'needs verification', 'badge warn'); badge.title = flag.message; row.append(badge);
  });
  const tools = node('span', undefined, 'bullet-tools');
  const edit = node('button', 'Edit'), remove = node('button', 'Delete');
  edit.type = remove.type = 'button';
  edit.addEventListener('click', () => toggleEdit('summary', bullet, body, edit).catch(showError));
  remove.addEventListener('click', async () => {
    if (!confirm('Delete this bullet from the notes?')) return;
    try { record = await api('/api/notes/' + selected + '/edit', {section: 'summary', item_id: bullet.id, delete: true}); lastSignature = ''; renderRecord(); }
    catch (error) { showError(error); }
  });
  tools.append(edit, remove);
  row.append(tools);
  li.append(row);
  if ((bullet.children || []).length) {
    const ul = node('ul'); bullet.children.forEach(child => ul.append(renderBullet(child, visuals))); li.append(ul);
  }
  if (visuals[bullet.id]) li.append(renderVisual('summary', bullet, visuals[bullet.id]));
  return li;
}
function renderItem(section, item) {
  const flags = item.flags || [], warn = flags.some(f => f.level === 'warning');
  const box = node('div', undefined, 'item' + (warn ? ' flagged' : ''));
  box.dataset.item = item.id;
  const head = node('div', undefined, 'item-head');
  head.append(node('span', item.id, 'item-id'));
  const text = node('div', undefined, 'item-text');
  const body = node('span', section === 'participants' ? item.name : item.text);
  if (section === 'alternatives') text.append(node('strong', item.option + ': '));
  text.append(body);
  if (item.kind) text.append(node('span', LABELS[item.kind] || item.kind, 'badge kind'));
  if (section === 'alternatives') text.append(node('span', LABELS[item.outcome], 'badge kind'));
  if (item.basis === 'interpreted') text.append(node('span', 'interpretation', 'badge interpreted'));
  if (item.edited) text.append(node('span', 'edited', 'badge edited'));
  if (warn) text.append(node('span', 'needs verification', 'badge warn'));
  const tools = node('div', undefined, 'item-tools');
  const edit = node('button', 'Edit'), remove = node('button', 'Delete');
  edit.type = remove.type = 'button';
  edit.addEventListener('click', () => toggleEdit(section, item, body, edit).catch(showError));
  remove.addEventListener('click', async () => {
    if (!confirm('Delete item ' + item.id + ' from the notes?')) return;
    try { record = await api('/api/notes/' + selected + '/edit', {section, item_id: item.id, delete: true}); lastSignature = ''; renderRecord(); refreshList(); }
    catch (error) { showError(error); }
  });
  tools.append(edit, remove);
  head.append(text, tools);
  box.append(head);

  const details = node('div', undefined, 'details');
  const add = (label, value, className) => {
    if (!value) return;
    const line = node('div', undefined, className); line.append(node('span', label + ': ', 'label'), node('span', value)); details.append(line);
  };
  if (section === 'participants') add('Role', item.role);
  if (section === 'decisions') { add('Parameters', (item.parameters || []).join('; ')); add('Reason', item.rationale || 'No reason stated in transcript.'); }
  if (section === 'alternatives') {
    add('Advantages', item.advantages.join('; ')); add('Disadvantages', item.disadvantages.join('; ')); add('Outcome reason', item.outcome_reason);
  }
  if (section === 'rationale') add('Factors', (item.factors || []).join(', '));
  if (section === 'open_issues') { add('Owner', item.owner || 'Not stated'); add('Due', item.due); }
  if (item.quote) add('Quote', '“' + item.quote + '”', 'quote');
  add('Uncertainty', item.uncertainty);
  flags.forEach(flag => add(flag.level === 'warning' ? 'Check' : 'Note', flag.message, 'flag ' + flag.level));
  if (details.childNodes.length) box.append(details);
  (item.visuals || []).forEach(visual => box.append(renderVisual(section, item, visual)));

  const refs = node('div', undefined, 'refs');
  const segments = Object.fromEntries(record.transcript.segments.map(s => [s.id, s]));
  (item.source_ids || []).forEach(ref => {
    if (!segments[ref]) return;
    const button = node('button', sourceLabel(segments[ref]), 'ref');
    button.type = 'button'; button.title = 'Show transcript excerpt';
    button.addEventListener('click', () => showSource(item, ref));
    refs.append(button);
  });
  box.append(refs);
  return box;
}
const imageCache = new Map();
async function imageUrl(name) {
  const key = selected + '/' + name;
  if (!imageCache.has(key)) {
    const response = await api('/api/notes/' + selected + '/visuals/' + encodeURIComponent(name), undefined, true);
    imageCache.set(key, URL.createObjectURL(await response.blob()));
  }
  return imageCache.get(key);
}
function visualCaption(visual) {
  const parts = ['Screenshot at ' + formatTime(visual.timestamp_s)];
  if (visual.cursor_source === 'pointing') parts.push('cursor resting here');
  else if (visual.cursor_norm) parts.push('cursor position');
  else parts.push('cursor not visible');
  if (visual.bbox) parts.push('discussed feature boxed');
  parts.push(visual.confidence + ' confidence');
  return parts.join(' · ');
}
function renderVisual(section, item, visual) {
  const figure = node('figure', undefined, 'visual');
  const shot = node('div', undefined, 'shot');
  shot.title = 'Click to enlarge';
  const image = node('img');
  image.alt = 'Screenshot supporting ' + item.id;
  shot.append(image);
  imageUrl(visual.file).then(url => { image.src = url; })
    .catch(() => shot.replaceChildren(node('span', 'Screenshot unavailable.', 'muted small')));
  const pct = value => (value * 100).toFixed(2) + '%';
  if (visual.bbox) {
    const [x0, y0, x1, y1] = visual.bbox, box = node('div', undefined, 'bbox');
    Object.assign(box.style, {left: pct(x0), top: pct(y0), width: pct(x1 - x0), height: pct(y1 - y0)});
    shot.append(box);
  }
  if (visual.cursor_norm) {
    const mark = node('div', undefined, 'cursor-mark' + (visual.cursor_source === 'pointing' ? ' pointing' : ''));
    Object.assign(mark.style, {left: pct(visual.cursor_norm[0]), top: pct(visual.cursor_norm[1])});
    mark.title = 'Cursor at ' + visual.cursor_norm.map(v => v.toFixed(3)).join(', ');
    shot.append(mark);
  }
  shot.addEventListener('click', () => figure.classList.toggle('expanded'));
  const caption = node('figcaption');
  caption.append(node('span', visualCaption(visual), 'visual-meta'));
  if (visual.rationale) caption.append(node('span', visual.rationale));
  const remove = node('button', 'Remove screenshot', 'link');
  remove.type = 'button';
  remove.addEventListener('click', async () => {
    if (!confirm('Remove the screenshot from item ' + item.id + '?')) return;
    try { record = await api('/api/notes/' + selected + '/edit', {section, item_id: item.id, remove_visual: true}); lastSignature = ''; renderRecord(); }
    catch (error) { showError(error); }
  });
  caption.append(remove);
  figure.append(shot, caption);
  return figure;
}
async function toggleEdit(section, item, body, button) {
  if (body.contentEditable !== 'true') {
    body.contentEditable = 'true'; body.classList.add('editing'); body.focus(); button.textContent = 'Save';
    return;
  }
  const text = body.textContent.trim();
  body.contentEditable = 'false'; button.textContent = 'Edit';
  record = await api('/api/notes/' + selected + '/edit', {section, item_id: item.id, text});
  lastSignature = ''; renderRecord(); refreshList();
}
function showSource(item, ref) {
  document.querySelectorAll('.focus').forEach(el => el.classList.remove('focus'));
  document.querySelectorAll('.ref.active').forEach(el => el.classList.remove('active'));
  const box = document.querySelector('[data-item="' + item.id + '"]');
  if (box) { box.classList.add('focus'); [...box.querySelectorAll('.ref')].forEach(b => { if (b.textContent.startsWith(ref)) b.classList.add('active'); }); }
  const segments = record.transcript.segments, ids = new Set(item.source_ids || [ref]);
  const index = segments.findIndex(s => s.id === ref);
  const first = Math.max(0, Math.min(...[...ids].map(id => segments.findIndex(s => s.id === id)).filter(i => i >= 0)) - 2);
  const last = Math.min(segments.length - 1, Math.max(index, ...[...ids].map(id => segments.findIndex(s => s.id === id))) + 2);
  const excerpt = $('sourceExcerpt');
  excerpt.replaceChildren(node('p', 'Supporting excerpt for ' + item.id + ':', 'small'));
  for (let i = first; i <= last; i++) excerpt.append(segmentNode(segments[i], ids.has(segments[i].id) ? 'hit' : 'context'));
  activeRef = ref;
  if (window.innerWidth < 1100) $('sourcePanel').scrollIntoView({behavior: 'smooth'});
}
function segmentNode(segment, className) {
  const el = node('div', undefined, 'segment ' + className);
  el.append(node('span', sourceLabel(segment) + (segment.line ? '  (line ' + segment.line + ')' : ''), 'meta'), node('span', segment.text));
  return el;
}
function renderTranscript() {
  const segments = record.transcript?.segments || [];
  $('fullTranscriptBox').classList.toggle('hidden', !segments.length);
  $('fullTranscript').replaceChildren(...segments.slice(0, 3000).map(s => segmentNode(s, '')));
}
async function download(kind) {
  const response = await api('/api/notes/' + selected + '/export.' + kind, undefined, true);
  const blob = await response.blob();
  const name = (response.headers.get('Content-Disposition') || '').match(/filename="([^"]+)"/)?.[1] || 'engineering-notes.' + kind;
  const url = URL.createObjectURL(blob), link = node('a');
  link.href = url; link.download = name; document.body.append(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

// ---------------------------------------------------------------- wiring
$('tabUpload').addEventListener('click', () => setMode('upload'));
$('tabPaste').addEventListener('click', () => setMode('paste'));
$('viewSummary').addEventListener('click', () => setView('summary'));
$('viewDetailed').addEventListener('click', () => setView('detailed'));
$('file').addEventListener('change', () => setFile($('file').files[0]));
const drop = $('dropZone');
['dragenter', 'dragover'].forEach(type => drop.addEventListener(type, event => { event.preventDefault(); drop.classList.add('over'); }));
['dragleave', 'drop'].forEach(type => drop.addEventListener(type, event => { event.preventDefault(); drop.classList.remove('over'); }));
drop.addEventListener('drop', event => setFile(event.dataTransfer.files[0]));
$('newNotes').addEventListener('click', showInput);
action('saveKey', async () => { await api('/api/key', {key: $('apiKey').value}); $('apiKey').value = ''; await refreshStatus(); });
action('generate', generate);
action('approve', async () => {
  record = await api('/api/notes/' + selected + '/approve', {approved: !record.review.approved_at}); renderRecord(); await refreshList();
});
action('exportMd', () => download('md'));
action('exportPdf', () => download('pdf'));
action('editTitle', async () => {
  const title = prompt('Meeting title', record.title); if (title === null) return;
  const project = prompt('Project / component (optional)', record.project || ''); if (project === null) return;
  record = await api('/api/notes/' + selected + '/rename', {title, project}); lastSignature = ''; renderRecord(); await refreshList();
});
action('retry', async () => { record = await api('/api/notes/' + selected + '/retry', {}); renderRecord(); });
action('deleteNotes', async () => {
  if (!confirm('Permanently delete these notes and the stored upload?')) return;
  await api('/api/notes/' + selected + '/delete', {}); showInput();
});
action('quit', async () => {
  await api('/api/quit', {}); clearInterval(poller);
  document.body.replaceChildren(node('p', 'Engineering Design Notes stopped. You can close this tab.'));
});
poller = setInterval(async () => {
  if (selected && record && !['ready', 'failed'].includes(record.state)) {
    try { await refreshNotes(); if (['ready', 'failed'].includes(record.state)) await refreshList(); } catch (error) { showError(error); }
  }
}, 1500);
setMode('upload');
refreshStatus().then(refreshList).catch(showError);
