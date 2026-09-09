const token = document.querySelector('meta[name="marlin-token"]').content;
const canvas = document.getElementById('brain');
const ctx = canvas.getContext('2d');
const input = document.getElementById('commandInput');
const messages = document.getElementById('messages');
let graph = { nodes: [], links: [] };
let nodeById = new Map();
let pendingAction = null;
let cameraStream = null;
let transform = { x: 0, y: 0, scale: 1 };
let selected = null;
let draggingNode = null;
let panning = false;
let pointer = { x: 0, y: 0 };
let streamingText = '';
let voiceChatActive = false;
let voiceChatPaused = false;
let voiceDraft = '';
let highlightedReminder = '';
function showTranscript(text) {
  if (!input.value || input.value === voiceDraft) { input.value = text; voiceDraft = text; }
}

function updateVoiceChat(active) {
  voiceChatActive = active;
  document.body.classList.toggle('voice-session', active);
  const button = document.getElementById('voiceChat');
  button.textContent = active ? 'End voice chat' : 'Voice chat';
  button.setAttribute('aria-pressed', String(active));
  document.getElementById('listen').disabled = active && !voiceChatPaused;
}

const colors = { file: '#8b82e8', folder: '#dce930', project: '#f0d92f', task: '#db5f9b', technology: '#40c9b5', note: '#c7d2d9', topic: '#ef8a3d' };
const linkColors = { contains: 'rgba(55,193,183,.62)', similar_files: 'rgba(61,139,154,.42)', belongs_to: 'rgba(224,232,48,.58)', uses: 'rgba(94,137,226,.5)', depends_on: 'rgba(219,95,155,.65)' };

async function api(path, options = {}) {
  const headers = { 'Content-Type': 'application/json', 'X-Marlin-Token': token, ...(options.headers || {}) };
  const response = await fetch(path, { ...options, headers });
  const body = await response.json();
  if (!response.ok) throw new Error(body.detail || `Request failed: ${response.status}`);
  return body;
}

function resize() {
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.floor(innerWidth * ratio);
  canvas.height = Math.floor(innerHeight * ratio);
  canvas.style.width = `${innerWidth}px`;
  canvas.style.height = `${innerHeight}px`;
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
}

function initializeGraph(payload) {
  const previous = nodeById;
  graph = payload;
  const root = (graph.root || '').replaceAll('\\', '/').toLowerCase().replace(/\/$/, '');
  for (const node of graph.nodes) {
    const path = (node.metadata?.path || '').replaceAll('\\', '/').toLowerCase();
    node.mainProject = node.type === 'folder' && path.slice(0, path.lastIndexOf('/')) === root;
    let hash = 0; for (const ch of path) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
    node.projectColor = `hsl(${hash % 360} 75% 72%)`;
  }
  nodeById = new Map(graph.nodes.map(node => [node.id, node]));
  graph.links = graph.links.map(link => ({ ...link, sourceNode: nodeById.get(link.source), targetNode: nodeById.get(link.target) })).filter(link => link.sourceNode && link.targetNode);
  for (const node of graph.nodes) { node.degree = 0; node.children = []; node.parent = null; }
  for (const link of graph.links) {
    link.sourceNode.degree += 1; link.targetNode.degree += 1;
    if (link.type === 'contains') { link.targetNode.parent = link.sourceNode; link.sourceNode.children.push(link.targetNode); }
  }

  const folders = graph.nodes.filter(node => node.type === 'folder').sort((a, b) => b.children.length - a.children.length || b.degree - a.degree);
  const semantic = graph.nodes.filter(node => !['folder', 'file'].includes(node.type));
  const centerX = innerWidth / 2, centerY = innerHeight / 2;
  semantic.forEach((node, index) => {
    const angle = index * 2.399963;
    const radius = 24 + Math.sqrt(index) * 34;
    node.anchorX = centerX + Math.cos(angle) * radius;
    node.anchorY = centerY + Math.sin(angle) * radius * .82;
  });
  folders.forEach((node, index) => {
    const angle = index * 2.399963 + .45;
    const radius = 60 + Math.sqrt(index) * 30;
    node.anchorX = centerX + Math.cos(angle) * radius;
    node.anchorY = centerY + Math.sin(angle) * radius * .86;
  });
  for (const folder of folders) {
    const children = folder.children.filter(node => node.type === 'file');
    const spread = Math.min(Math.PI * 1.72, .52 + children.length * .055);
    const start = Math.atan2(folder.anchorY - centerY, folder.anchorX - centerX) - spread / 2;
    children.forEach((node, index) => {
      const ring = Math.floor(index / 34);
      const slot = index % 34;
      const slots = Math.min(34, children.length - ring * 34);
      const angle = start + spread * ((slot + .5) / Math.max(1, slots));
      const radius = 30 + ring * 19 + Math.min(28, children.length * .15);
      node.anchorX = folder.anchorX + Math.cos(angle) * radius;
      node.anchorY = folder.anchorY + Math.sin(angle) * radius;
    });
  }
  graph.nodes.filter(node => node.anchorX === undefined).forEach((node, index) => {
    const parent = node.parent;
    const angle = index * 2.399963;
    node.anchorX = (parent?.anchorX || centerX) + Math.cos(angle) * (38 + Math.sqrt(index) * 6);
    node.anchorY = (parent?.anchorY || centerY) + Math.sin(angle) * (38 + Math.sqrt(index) * 6);
  });
  for (const node of graph.nodes) {
    const old = previous.get(node.id);
    node.x = Number.isFinite(old?.x) ? old.x : node.anchorX + (Math.random() - .5) * 18;
    node.y = Number.isFinite(old?.y) ? old.y : node.anchorY + (Math.random() - .5) * 18;
    if (old?.userPlaced) {
      node.userPlaced = true; node.anchorX = old.anchorX; node.anchorY = old.anchorY;
    }
    node.vx = 0; node.vy = 0;
  }
  if (!previous.size) fitGraph();
}

function simulate() {
  for (const node of graph.nodes) {
    if (node === draggingNode) continue;
    const anchorForce = node.type === 'folder' ? .0011 : node.type === 'file' ? .0017 : .00075;
    node.vx += (node.anchorX - node.x) * anchorForce;
    node.vy += (node.anchorY - node.y) * anchorForce;
  }
  for (const link of graph.links) {
    const a = link.sourceNode, b = link.targetNode;
    const dx = b.x - a.x, dy = b.y - a.y;
    const distance = Math.max(1, Math.hypot(dx, dy));
    const desired = link.type === 'contains' ? (b.type === 'file' ? 48 : 108) : link.type === 'similar_files' ? 92 : 128;
    const force = (distance - desired) * (link.type === 'contains' ? .00055 : link.type === 'similar_files' ? .0003 : .00018);
    const fx = dx / distance * force, fy = dy / distance * force;
    if (a !== draggingNode) { a.vx += fx; a.vy += fy; }
    if (b !== draggingNode) { b.vx -= fx; b.vy -= fy; }
  }
  const cells = new Map();
  const cellSize = 34;
  for (const node of graph.nodes) {
    const key = `${Math.floor(node.x / cellSize)},${Math.floor(node.y / cellSize)}`;
    const peers = cells.get(key) || [];
    for (const peer of peers.slice(-18)) {
      const dx = node.x - peer.x || .1, dy = node.y - peer.y || .1;
      const distance = Math.max(3, Math.hypot(dx, dy));
      const force = Math.min(.42, 22 / (distance * distance));
      node.vx += dx / distance * force; node.vy += dy / distance * force;
      peer.vx -= dx / distance * force; peer.vy -= dy / distance * force;
    }
    peers.push(node); cells.set(key, peers);
  }
  for (const node of graph.nodes) {
    if (node === draggingNode) continue;
    node.vx *= .88; node.vy *= .88;
    node.x += node.vx; node.y += node.vy;
  }
}

function draw() {
  simulate();
  ctx.clearRect(0, 0, innerWidth, innerHeight);
  ctx.save();
  ctx.translate(transform.x, transform.y);
  ctx.scale(transform.scale, transform.scale);
  const connected = selected ? new Set([selected.id]) : null;
  if (selected) for (const link of graph.links) if (link.source === selected.id || link.target === selected.id) { connected.add(link.source); connected.add(link.target); }
  for (const link of graph.links) {
    const highlighted = !selected || link.source === selected.id || link.target === selected.id;
    ctx.globalAlpha = selected ? (highlighted ? 1 : .055) : (link.type === 'contains' ? .42 : .2);
    ctx.lineWidth = (highlighted && selected ? 1.55 : .55) / transform.scale;
    ctx.strokeStyle = linkColors[link.type] || 'rgba(105,190,194,.34)';
    ctx.beginPath(); ctx.moveTo(link.sourceNode.x, link.sourceNode.y); ctx.lineTo(link.targetNode.x, link.targetNode.y); ctx.stroke();
  }
  for (const node of graph.nodes) {
    const degree = node.degree || 0;
    ctx.globalAlpha = connected && !connected.has(node.id) ? .14 : 1;
    const radius = node.mainProject ? 10 : node === selected ? 8 : node.type === 'folder' ? Math.min(7, 3.4 + Math.sqrt(degree) * .38) : Math.min(5.2, 1.35 + Math.sqrt(degree) * .58);
    ctx.fillStyle = node.mainProject ? node.projectColor : colors[node.type] || '#9aa8b2';
    ctx.shadowColor = ctx.fillStyle; ctx.shadowBlur = node === selected ? 18 : node.type === 'folder' ? 8 : 2;
    ctx.beginPath(); ctx.arc(node.x, node.y, radius, 0, Math.PI * 2); ctx.fill();
    ctx.shadowBlur = 0;
  }
  const labelBoxes = [];
  const labelled = graph.nodes.filter(n => n === selected || n.mainProject || (n.degree >= 12 && transform.scale > 1.15));
  labelled.sort((a, b) => Number(b === selected) - Number(a === selected) || Number(b.mainProject) - Number(a.mainProject));
  for (const node of labelled) {
    const size = (node.mainProject ? 15 : 11) / transform.scale;
    ctx.font = `${node.mainProject ? '600 ' : ''}${size}px Segoe UI`;
    const text = node === selected ? node.label : node.label.slice(0, 36);
    const box = {x: node.x + 13, y: node.y - size, w: ctx.measureText(text).width + 8, h: size + 6};
    if (node !== selected && labelBoxes.some(b => box.x < b.x+b.w && box.x+box.w > b.x && box.y < b.y+b.h && box.y+box.h > b.y)) continue;
    labelBoxes.push(box);
    ctx.globalAlpha = connected && !connected.has(node.id) ? .2 : 1;
    ctx.fillStyle = node.mainProject ? node.projectColor : '#dce8ee';
    ctx.fillText(text, box.x, node.y);
  }
  ctx.globalAlpha = 1;
  ctx.restore();
  requestAnimationFrame(draw);
}

function fitGraph() {
  if (!graph.nodes.length) return;
  const xs = graph.nodes.map(node => node.anchorX ?? node.x), ys = graph.nodes.map(node => node.anchorY ?? node.y);
  const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
  const scale = Math.max(.25, Math.min(1.1, Math.min((innerWidth - 80) / Math.max(1, maxX - minX), (innerHeight - 80) / Math.max(1, maxY - minY))));
  transform.scale = scale;
  transform.x = innerWidth / 2 - ((minX + maxX) / 2) * scale;
  transform.y = innerHeight / 2 - ((minY + maxY) / 2) * scale;
}

function graphPoint(event) {
  return { x: (event.clientX - transform.x) / transform.scale, y: (event.clientY - transform.y) / transform.scale };
}
function nearestNode(point) {
  let nearest = null, distance = 14 / transform.scale;
  for (const node of graph.nodes) {
    const d = Math.hypot(node.x - point.x, node.y - point.y);
    if (d < distance) { nearest = node; distance = d; }
  }
  return nearest;
}
canvas.addEventListener('pointerdown', event => {
  pointer = { x: event.clientX, y: event.clientY };
  draggingNode = nearestNode(graphPoint(event));
  selected = draggingNode || null;
  panning = !draggingNode;
  canvas.setPointerCapture(event.pointerId);
});
canvas.addEventListener('pointermove', event => {
  const hover = nearestNode(graphPoint(event));
  canvas.title = hover ? `${hover.label}\n${hover.metadata?.path || ''}` : '';
  if (draggingNode) { const p = graphPoint(event); draggingNode.x = p.x; draggingNode.y = p.y; draggingNode.vx = 0; draggingNode.vy = 0; }
  else if (panning) { transform.x += event.clientX - pointer.x; transform.y += event.clientY - pointer.y; }
  pointer = { x: event.clientX, y: event.clientY };
});
function releaseGraph() {
  if (draggingNode) { draggingNode.anchorX = draggingNode.x; draggingNode.anchorY = draggingNode.y; draggingNode.userPlaced = true; }
  draggingNode = null; panning = false; selected = null;
}
for (const event of ['pointerup', 'pointercancel', 'lostpointercapture']) canvas.addEventListener(event, releaseGraph);
document.addEventListener('keydown', event => { if (event.key === 'Escape') releaseGraph(); });
canvas.addEventListener('wheel', event => {
  event.preventDefault();
  const before = graphPoint(event);
  const factor = event.deltaY < 0 ? 1.1 : .9;
  transform.scale = Math.max(.18, Math.min(4, transform.scale * factor));
  transform.x = event.clientX - before.x * transform.scale;
  transform.y = event.clientY - before.y * transform.scale;
}, { passive: false });
canvas.addEventListener('dblclick', () => fitGraph());

function addMessage(text, role = 'assistant', id = '') {
  if (id) document.getElementById(id)?.remove();
  const element = document.createElement('div');
  element.className = `message ${role}`; element.textContent = text;
  if (id) element.id = id;
  messages.appendChild(element);
  while (messages.children.length > 6) messages.firstElementChild.remove();
  messages.scrollTop = messages.scrollHeight;
}

function showPending(action) {
  pendingAction = action;
  document.getElementById('confirmLabel').textContent = action.label;
  document.getElementById('confirmTarget').textContent = `Target: ${action.target}`;
  const diff = document.getElementById('confirmDiff'); diff.textContent = action.preview || 'This action can change local state.'; diff.style.display = action.preview ? 'block' : 'none';
  document.getElementById('confirmPanel').classList.add('active');
}
function hidePending() { pendingAction = null; document.getElementById('confirmPanel').classList.remove('active'); }

let feedbackTimer;
function feedback(message, error = false) {
  const toast = document.getElementById('controlFeedback');
  toast.textContent = message;
  toast.className = error ? 'visible error' : 'visible';
  clearTimeout(feedbackTimer);
  feedbackTimer = setTimeout(() => toast.classList.remove('visible'), error ? 6000 : 3000);
}
function busy(button, value) {
  button.disabled = value;
  button.setAttribute('aria-busy', String(value));
}

async function submitCommand(text) {
  feedback('Request received');
  addMessage(text, 'user'); addMessage('MARLIN is thinking…', 'thinking', 'thinking');
  try {
    await api('/api/voice/stop', { method: 'POST' });
    const result = await api('/api/commands', { method: 'POST', body: JSON.stringify({ text, source: 'ui' }) });
    document.getElementById('thinking')?.remove();
    if (result.message) addMessage(result.message, 'assistant');
    if (result.pending) showPending(result.pending);
    await handleClientAction(result.client_action);
    refreshState();
  } catch (error) { document.getElementById('thinking')?.remove(); addMessage(error.message, 'assistant'); }
}

document.getElementById('commandForm').addEventListener('submit', event => {
  event.preventDefault(); const text = input.value.trim(); if (!text) return; input.value = ''; voiceDraft = ''; submitCommand(text);
});
document.getElementById('approveAction').addEventListener('click', async () => {
  if (!pendingAction) return; const id = pendingAction.id; hidePending();
  const result = await api(`/api/actions/${id}/approve`, { method: 'POST' }); addMessage(result.message); await handleClientAction(result.client_action); refreshState();
});
document.getElementById('cancelAction').addEventListener('click', async () => {
  if (pendingAction) await api(`/api/actions/${pendingAction.id}/cancel`, { method: 'POST' }); hidePending(); addMessage('Action cancelled.');
});
document.getElementById('stopVoice').addEventListener('click', async event => {
  const button = event.currentTarget;
  busy(button, true); feedback('Stopping voice...');
  document.getElementById('voiceLevel').style.width = '0%';
  try { await api('/api/voice/stop', { method: 'POST' }); feedback('Voice stopped'); document.getElementById('voiceState').textContent = 'Voice stopped'; }
  catch (error) { feedback(error.message, true); }
  finally { busy(button, false); }
});
document.getElementById('talkNow').addEventListener('click', async event => {
  const button = event.currentTarget;
  busy(button, true);
  feedback('Interrupting reply...');
  try {
    const result = await api('/api/voice/interrupt', {method: 'POST'});
    updateVoiceChat(result.active);
    document.getElementById('thinking')?.remove();
    feedback('Reply stopped. Voice chat is resuming.');
  } catch (error) { feedback(error.message, true); }
  finally { busy(button, false); }
});
for (const [buttonId, panelId] of [['toggleStatus', 'statusPanel'], ['toggleRoutine', 'routinePanel'], ['toggleSchedule', 'schedulePanel'], ['toggleMemory', 'memoryPanel'], ['toggleResearch', 'researchPanel'], ['toggleProlog', 'prologPanel'], ['toggleMessaging', 'messagingPanel']]) {
  const button = document.getElementById(buttonId);
  const panel = document.getElementById(panelId);
  button.addEventListener('click', () => {
    const open = panel.classList.toggle('revealed');
    button.setAttribute('aria-expanded', String(open));
  });
}

function row(title, detail = '', small = '') {
  const item = document.createElement('div'); item.className = 'agent-row';
  const heading = document.createElement('strong'); heading.textContent = title; item.append(heading);
  if (detail) { const body = document.createElement('div'); body.textContent = detail; item.append(body); }
  if (small) { const note = document.createElement('small'); note.textContent = small; item.append(note); }
  return item;
}

function renderAgentPanels(state) {
  const schedule = document.getElementById('scheduleContent'); schedule.replaceChildren();
  const plan = state.schedule_plan;
  const plannedIds = new Set((plan?.blocks || []).map(block => block.item_id));
  for (const block of plan?.blocks || []) schedule.append(row(block.title, `${new Date(block.start_at).toLocaleString()} – ${new Date(block.end_at).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'})}`, block.status));
  for (const item of state.schedule_items || []) {
    if (plannedIds.has(item.id)) continue;
    const timing = item.start_at ? `${new Date(item.start_at).toLocaleString()} – ${new Date(item.end_at).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'})}` : item.deadline_at ? `Due ${new Date(item.deadline_at).toLocaleString()}` : `${item.duration_minutes} minutes · awaiting day plan`;
    schedule.append(row(item.title, timing, `${item.kind} · ${item.status}${item.recurrence !== 'none' ? ` · ${item.recurrence}` : ''}`));
  }
  if (plan?.status === 'preview') {
    const actions = document.createElement('div'); actions.className = 'agent-actions';
    for (const [label, action] of [['Apply', 'apply'], ['Discard', 'discard']]) { const button = document.createElement('button'); button.textContent = label; button.onclick = async () => { await api(`/api/schedule/plans/${plan.id}/${action}`, {method:'POST'}); await refreshState(); }; actions.append(button); }
    schedule.append(actions);
  }
  if (!schedule.children.length) schedule.textContent = 'No schedule yet. Ask MARLIN to plan your day.';

  const memory = document.getElementById('memoryContent'); memory.replaceChildren();
  for (const preference of state.preferences || []) {
    const item = row(preference.value, preference.category.replaceAll('_', ' '), `${Math.round(preference.confidence * 100)}% · ${preference.active ? 'active' : `${preference.observations}/3 observations`}`);
    if (preference.active) { const forget = document.createElement('button'); forget.textContent = 'Forget'; forget.onclick = async () => { await api(`/api/preferences/${preference.id}`, {method:'DELETE'}); await refreshState(); }; item.append(forget); }
    memory.append(item);
  }
  if (!memory.children.length) memory.textContent = 'No learned preferences.';

  const research = document.getElementById('researchContent'); research.replaceChildren();
  for (const run of state.research || []) {
    research.append(row(run.query, run.summary || '', new Date(run.created_at).toLocaleString()));
    for (const source of run.sources || []) { const item = document.createElement('div'); item.className = 'agent-row'; const link = document.createElement('a'); link.className = 'source-link'; link.href = source.url; link.target = '_blank'; link.rel = 'noopener'; link.textContent = `[${source.rank}] ${source.title}`; const score = document.createElement('small'); score.textContent = ` ${source.domain}${source.prolog_score == null ? '' : ` · Prolog ${source.prolog_score}`}`; item.append(link, score); research.append(item); }
  }
  if (!research.children.length) research.textContent = 'No research yet. Use “Research …”.';

  const prolog = document.getElementById('prologContent'); prolog.replaceChildren();
  const activity = state.prolog_activity || {};
  const predicate = activity.predicate || activity.query;
  if (predicate) { prolog.append(row(predicate, activity.facts_source || `Relevant facts: ${activity.facts ?? 0}`, (activity.rules || []).join(', '))); const proof = document.createElement('pre'); proof.className = 'proof'; proof.textContent = JSON.stringify(activity.proof || activity.result || activity.explanations, null, 2); prolog.append(proof); }
  else prolog.textContent = 'No predicate invoked yet.';

  renderMessaging(state.telegram || {});
}

function renderMessaging(telegram) {
  const container = document.getElementById('messagingContent'); container.replaceChildren();
  const connection = telegram.connected ? 'Connected' : telegram.configured ? 'Offline' : 'Disabled';
  container.append(row(connection, telegram.bot ? `@${telegram.bot}` : '', telegram.error || ''));
  if (telegram.owner) container.append(row('Owner', telegram.owner.display_name || String(telegram.owner.user_id)));
  else {
    const pair = document.createElement('button'); pair.textContent = 'Create pairing code';
    pair.disabled = !telegram.configured || !telegram.enabled;
    if (pair.disabled) pair.title = 'Enable Telegram and add a BotFather token in .env first.';
    pair.onclick = async () => {
      busy(pair, true);
      try {
        const result = await api('/api/telegram/pair-code', {method:'POST'});
        const item = row(`Pair code ${result.code}`, `Expires ${new Date(result.expires_at).toLocaleTimeString()}`);
        container.append(item); feedback('Telegram pairing code created');
      } catch (error) { feedback(error.message, true); }
      finally { busy(pair, false); }
    };
    container.append(pair);
  }
  for (const contact of telegram.contacts || []) {
    const item = row(contact.alias || contact.display_name || String(contact.user_id), contact.role, contact.active ? 'active' : 'pending');
    if (contact.role === 'pending') {
      const controls = document.createElement('div'); controls.className = 'telegram-contact-actions';
      const alias = document.createElement('input'); alias.placeholder = 'Contact alias'; alias.maxLength = 80;
      const add = document.createElement('button'); add.textContent = 'Add';
      add.onclick = async () => { if (!alias.value.trim()) return alias.focus(); try { await api(`/api/telegram/contacts/${contact.user_id}/approve`, {method:'POST', body:JSON.stringify({alias:alias.value.trim()})}); feedback('Telegram contact added'); await refreshState(); } catch (error) { feedback(error.message, true); } };
      const reject = document.createElement('button'); reject.textContent = 'Reject';
      reject.onclick = async () => { try { await api(`/api/telegram/contacts/${contact.user_id}`, {method:'DELETE'}); feedback('Telegram contact rejected'); await refreshState(); } catch (error) { feedback(error.message, true); } };
      controls.append(alias, add, reject); item.append(controls);
    }
    container.append(item);
  }
  for (const draft of (telegram.drafts || []).filter(item => item.status === 'pending')) {
    const item = row(`To ${draft.recipient_alias}`, draft.content, `Expires ${new Date(draft.expires_at).toLocaleTimeString()}`);
    const controls = document.createElement('div'); controls.className = 'agent-actions';
    for (const action of ['approve', 'cancel']) {
      const button = document.createElement('button'); button.textContent = action === 'approve' ? 'Send' : 'Cancel';
      button.onclick = async () => { try { await api(`/api/telegram/drafts/${draft.id}/${action}`, {method:'POST'}); feedback(action === 'approve' ? 'Telegram message sent' : 'Telegram draft cancelled'); await refreshState(); } catch (error) { feedback(error.message, true); } };
      controls.append(button);
    }
    item.append(controls); container.append(item);
  }
  for (const delivery of (telegram.deliveries || []).slice(0, 8)) {
    const label = delivery.kind.replaceAll('_', ' ');
    container.append(row(label, delivery.content, delivery.status === 'failed' ? delivery.error : delivery.status));
  }
}
document.getElementById('listen').addEventListener('click', async event => {
  const button = event.currentTarget;
  button.textContent = 'Listening…'; busy(button, true); feedback('Listening. Go ahead.');
  try {
    const result = await api('/api/voice/listen', { method: 'POST', body: JSON.stringify({ execute: true }) });
    if (result.resumed) { voiceChatPaused = false; updateVoiceChat(true); feedback('Listening'); }
    if (result.cancelled) feedback('Listening cancelled');
    else if (result.requires_clarification) { showTranscript(result.text || ''); input.focus(); feedback(result.error, true); }
    else if (result.result) {
      addMessage(result.text, 'user');
      if (result.result.message) addMessage(result.result.message);
      if (result.result.pending) showPending(result.result.pending);
      await handleClientAction(result.result.client_action);
    }
    else if (result.error) { addMessage(result.error); feedback(result.error, true); }
  }
  catch (error) { addMessage(error.message); }
  finally { button.textContent = 'Listen'; busy(button, false); }
});
document.getElementById('microphone').addEventListener('change', async event => {
  localStorage.setItem('marlin-microphone', event.target.value);
  await api('/api/voice/device', { method: 'POST', body: JSON.stringify({ device: event.target.value }) });
});
document.getElementById('voiceChat').addEventListener('click', async event => {
  const button = event.currentTarget;
  busy(button, true);
  const starting = !voiceChatActive;
  try {
    const result = await api(`/api/voice/chat/${starting ? 'start' : 'stop'}`, { method: 'POST' });
    updateVoiceChat(result.active);
    feedback(result.active ? 'Voice chat started' : 'Voice chat ended');
  } catch (error) { feedback(error.message, true); }
  finally { busy(button, false); }
});

async function openCamera() {
  const panel = document.getElementById('cameraPanel');
  if (cameraStream) { feedback('Camera is already open'); return; }
  feedback('Opening camera...');
  try { cameraStream = await navigator.mediaDevices.getUserMedia({ video: true, audio: false }); document.getElementById('cameraVideo').srcObject = cameraStream; panel.classList.add('active'); feedback('Camera opened'); }
  catch (error) { feedback(`Camera unavailable: ${error.message}`, true); }
}
function closeCamera() { cameraStream?.getTracks().forEach(track => track.stop()); cameraStream = null; document.getElementById('cameraPanel').classList.remove('active'); feedback('Camera closed'); }
async function handleClientAction(action) {
  if (action === 'open_camera') await openCamera();
  if (action === 'close_camera') closeCamera();
  if (action === 'open_camera_native') feedback('Windows Camera opened');
  if (action === 'close_camera_native') { closeCamera(); feedback('Windows Camera closed'); }
}
document.getElementById('closeCamera').addEventListener('click', closeCamera);

function renderRoutine(state) {
  const container = document.getElementById('routineContent');
  const signature = JSON.stringify([state.reminders, state.alarms, state.reminder_storage, highlightedReminder, new Date().toDateString()]);
  if (container.dataset.signature === signature) return;
  container.dataset.signature = signature;
  container.replaceChildren();
  const groups = new Map(['Today', 'Upcoming', 'Overdue', 'Unscheduled', 'Completed'].map(name => [name, []]));
  const now = new Date();
  for (const item of state.reminders) {
    const due = item.due_at ? new Date(item.due_at) : null;
    const group = item.completed ? 'Completed' : !due ? 'Unscheduled' : due < now ? 'Overdue' : due.toDateString() === now.toDateString() ? 'Today' : 'Upcoming';
    groups.get(group).push(item);
  }
  const stamp = value => new Date(value).toLocaleString('en-GB', {dateStyle: 'medium', timeStyle: 'short'}) + ` (${Intl.DateTimeFormat().resolvedOptions().timeZone})`;
  for (const [group, items] of groups) {
    if (!items.length) continue;
    const heading = document.createElement('h3'); heading.textContent = group; container.append(heading);
    for (const item of items.sort((a,b) => (a.due_at || '').localeCompare(b.due_at || ''))) {
      const row = document.createElement('article'); row.className = 'reminder-row'; row.classList.toggle('new-reminder', item.id === highlightedReminder);
      const title = document.createElement('strong'); title.textContent = item.text;
      const time = document.createElement('p'); time.textContent = item.due_at ? stamp(item.due_at) : 'No scheduled time';
      const status = document.createElement('small'); status.textContent = item.completed ? 'Completed' : item.notified_at ? 'Notified' : 'Pending';
      const details = document.createElement('details'); const summary = document.createElement('summary'); summary.textContent = 'Saved locally in MARLIN';
      const location = document.createElement('p'); location.textContent = `${state.reminder_storage}\nReminder ID: ${item.id}`;
      details.append(summary, location); row.append(title, time, status, details);
      if (!item.completed) {
        const controls = document.createElement('div'); controls.className = 'reminder-controls';
        for (const [action, symbol, label] of [['complete', '✓', 'Complete reminder'], ['snooze', '↻', 'Snooze 5 minutes']]) {
          const button = document.createElement('button'); button.textContent = symbol; button.title = label; button.setAttribute('aria-label', label);
          button.onclick = async () => { busy(button, true); try { await api(`/api/reminders/${encodeURIComponent(item.id)}/${action}`, {method: 'POST', body: action === 'snooze' ? JSON.stringify({minutes: 5}) : undefined}); await refreshState(); } catch (error) { feedback(error.message, true); busy(button, false); } };
          controls.append(button);
        }
        row.append(controls);
      }
      container.append(row);
    }
  }
  for (const alarm of state.alarms) { const row = document.createElement('p'); row.textContent = `Alarm: ${alarm.label} · ${stamp(alarm.due_at)}`; container.append(row); }
  if (!state.reminders.length && !state.alarms.length) container.textContent = 'No alarms or reminders.';
}

async function refreshState() {
  const state = await api('/api/state');
  document.getElementById('statusGrid').innerHTML = [
    ['state', state.state], ['model', state.model.loaded ? state.model.model : state.model.available ? 'model missing' : 'offline'],
    ['last reply', state.model.tokens ? `${state.model.tokens} tokens · ${state.model.total_ms} ms` : 'waiting'],
    ['prolog', state.prolog.available ? 'ready' : 'offline'], ['voice', `${state.voice.stt} / ${state.voice.tts}`],
    ['index', state.index_progress?.indexed ? `${state.index_progress.indexed} files` : state.index], ['brain', `${state.entities} nodes · ${state.relationships} links`]
  ].map(([key, value]) => `<div class="status-row"><strong>${key}</strong><span class="state-${state.state}">${value}</span></div>`).join('');
  renderRoutine(state);
  renderAgentPanels(state);
  const voiceState = document.getElementById('voiceState');
  if (state.voice.wake_word && !state.voice_chat) voiceState.textContent = state.voice.wake_status === 'ready' ? 'Say “Hey MARLIN”' : `Wake: ${state.voice.wake_status}`;
  const select = document.getElementById('microphone');
  voiceChatPaused = Boolean(state.voice_chat_paused);
  updateVoiceChat(Boolean(state.voice_chat));
  select.title = select.value ? 'Selected microphone' : `System default: ${state.voice.default_microphone_name || 'Windows input device'}`;
  if (!select.dataset.ready) {
    const saved = localStorage.getItem('marlin-microphone') ?? '';
    select.innerHTML = `<option value="">System default</option>` + (state.voice.microphones || []).map(item => `<option value="${item.id}">${item.name}</option>`).join('');
    select.value = saved;
    select.dataset.ready = 'true';
    if (saved) api('/api/voice/device', { method: 'POST', body: JSON.stringify({ device: saved }) }).catch(() => {});
  }
}
async function refreshGraph() { initializeGraph(await api('/api/graph?limit=1800')); }

function connectEvents() {
  const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
  const socket = new WebSocket(`${protocol}://${location.host}/api/events?token=${encodeURIComponent(token)}`);
  socket.onmessage = event => {
    const packet = JSON.parse(event.data);
    if (packet.type === 'assistant.thinking') { streamingText = ''; addMessage('MARLIN is thinking…', 'thinking', 'thinking'); }
    if (packet.type === 'assistant.delta') {
      streamingText += packet.data.text || '';
      let stream = document.getElementById('streaming');
      if (!stream) { addMessage('', 'assistant', 'streaming'); stream = document.getElementById('streaming'); }
      stream.textContent = streamingText;
      document.getElementById('thinking')?.remove();
    }
    if (packet.type === 'assistant.timing') document.getElementById('voiceState').textContent = `Replying in ${(packet.data.first_token_ms / 1000).toFixed(1)}s`;
    if (packet.type === 'assistant.done') { document.getElementById('thinking')?.remove(); document.getElementById('streaming')?.remove(); streamingText = ''; }
    if (packet.type === 'action.preview') showPending(packet.data.action);
    if (packet.type === 'tool.call') addMessage(`Tool: ${packet.data.name}`, 'thinking');
    if (packet.type === 'prolog.result') addMessage(`Prolog: ${packet.data.query}`, 'thinking');
    if (packet.type === 'graph.refresh') refreshGraph();
    if (packet.type === 'voice.state') document.getElementById('voiceState').textContent = `Voice ${packet.data.state}`;
    if (packet.type === 'voice.error') feedback(packet.data.error, true);
    if (packet.type === 'voice.timing') document.getElementById('voiceState').textContent = `Transcribed in ${(packet.data.transcription_ms / 1000).toFixed(1)}s`;
    if (packet.type === 'voice.clarification') {
      showTranscript(packet.data.text || '');
      feedback(packet.data.error || 'Check the transcript before running it.', true);
    }
    if (packet.type === 'voice.chat.retrying') { showTranscript(packet.data.text || ''); document.getElementById('voiceState').textContent = 'Listening again'; }
    if (packet.type === 'voice.chat.interrupted' || packet.type === 'voice.barge_in') {
      document.getElementById('thinking')?.remove();
      document.getElementById('streaming')?.remove();
      streamingText = '';
      document.getElementById('voiceState').textContent = 'Listening to you';
      feedback('Interrupted. Go ahead.');
    }
    if (packet.type === 'voice.chat.paused') { voiceChatPaused = true; showTranscript(packet.data.text || ''); updateVoiceChat(true); input.focus(); }
    if (packet.type === 'voice.chat.resumed') { voiceChatPaused = false; updateVoiceChat(true); }
    if (packet.type === 'voice.chat') {
      updateVoiceChat(packet.data.active);
      document.getElementById('wake-listening')?.remove();
      if (!packet.data.active) feedback('Voice chat ended');
    }
    if (packet.type === 'voice.chat.heard') { showTranscript(packet.data.text); addMessage(packet.data.text, 'user'); }
    if (packet.type === 'voice.chat.result') {
      if (packet.data.message) addMessage(packet.data.message, 'assistant');
      if (packet.data.pending) showPending(packet.data.pending);
      else hidePending();
      handleClientAction(packet.data.client_action);
    }
    if (packet.type === 'voice.chat.cancelled' && pendingAction?.id === packet.data.action_id) hidePending();
    if (packet.type === 'voice.level') document.getElementById('voiceLevel').style.width = `${Math.round((packet.data.level || 0) * 100)}%`;
    if (packet.type === 'wake.ready') document.getElementById('voiceState').textContent = 'Say “Hey MARLIN”';
    if (packet.type === 'wake.acknowledged') { feedback('Heard you. Listening...'); document.getElementById('voiceState').textContent = 'Listening'; }
    if (packet.type === 'wake.detected') addMessage('Yes, sir? Listening…', 'thinking', 'wake-listening');
    if (packet.type === 'wake.heard') { document.getElementById('wake-listening')?.remove(); addMessage(packet.data.text, 'user'); }
    if (packet.type === 'wake.error') { document.getElementById('wake-listening')?.remove(); addMessage(packet.data.error, 'assistant'); }
    if (packet.type === 'wake.result') {
      document.getElementById('wake-listening')?.remove();
      if (packet.data.message) addMessage(packet.data.message, 'assistant');
      if (packet.data.pending) showPending(packet.data.pending);
      handleClientAction(packet.data.client_action);
    }
    if (packet.type === 'assistant.interrupted') { document.getElementById('thinking')?.remove(); document.getElementById('streaming')?.remove(); streamingText = ''; }
    if (packet.type === 'reminder.created' || packet.type === 'reminder.fired') {
      highlightedReminder = packet.data.reminder?.id;
      document.getElementById('routinePanel').classList.add('revealed');
      document.getElementById('toggleRoutine').setAttribute('aria-expanded', 'true');
    }
    if (packet.type === 'alarm.created') {
      document.getElementById('routinePanel').classList.add('revealed');
      document.getElementById('toggleRoutine').setAttribute('aria-expanded', 'true');
    }
    if (packet.type === 'schedule.item.created' || packet.type === 'schedule.plan.preview' || packet.type === 'schedule.plan.applied') {
      document.getElementById('schedulePanel').classList.add('revealed');
      document.getElementById('toggleSchedule').setAttribute('aria-expanded', 'true');
    }
    if (packet.type === 'research.completed') {
      document.getElementById('researchPanel').classList.add('revealed');
      document.getElementById('toggleResearch').setAttribute('aria-expanded', 'true');
      feedback('Internet research complete');
    }
    if (packet.type === 'reminder.fired') { highlightedReminder = packet.data.reminder.id; addMessage(`Reminder: ${packet.data.reminder.text}`); feedback(`Reminder: ${packet.data.reminder.text}`); }
    if (packet.type === 'routine.retrying') feedback('Reminder service is retrying after a database delay', true);
    if (['assistant.state','voice.state','wake.detected','wake.result','index.progress','alarm.created','alarm.fired','reminder.created','reminder.updated','reminder.fired','schedule.item.created','schedule.plan.preview','schedule.plan.applied','schedule.plan.discarded','preference.learned','preference.observed','preference.forgotten','research.completed','prolog.result','telegram.connected','telegram.error','telegram.owner.paired','telegram.contact.approved','telegram.contact.revoked','telegram.draft.created','telegram.draft.sent','telegram.draft.cancelled','telegram.draft.failed'].includes(packet.type)) refreshState();
    if (packet.type === 'alarm.fired') addMessage(`${packet.data.alarm.label}. Would you like five more minutes?`);
  };
  socket.onclose = () => setTimeout(connectEvents, 1200);
}

function makeMoveable(element) {
  const header = element.querySelector('header') || element;
  const key = `marlin-position-${element.dataset.key}`;
  const saved = JSON.parse(localStorage.getItem(key) || 'null');
  if (saved) { element.style.left = `${saved.x}px`; element.style.top = `${saved.y}px`; element.style.right = 'auto'; element.style.bottom = 'auto'; element.style.transform = 'none'; }
  header.addEventListener('pointerdown', event => {
    if (event.target.closest('button')) return;
    const box = element.getBoundingClientRect(); const offset = { x: event.clientX - box.left, y: event.clientY - box.top };
    header.setPointerCapture(event.pointerId);
    const move = e => { const x = Math.max(0, Math.min(innerWidth - box.width, e.clientX - offset.x)); const y = Math.max(0, Math.min(innerHeight - box.height, e.clientY - offset.y)); element.style.left = `${x}px`; element.style.top = `${y}px`; element.style.right = 'auto'; element.style.bottom = 'auto'; element.style.transform = 'none'; };
    const stop = () => { header.removeEventListener('pointermove', move); localStorage.setItem(key, JSON.stringify({ x: parseFloat(element.style.left), y: parseFloat(element.style.top) })); };
    header.addEventListener('pointermove', move); header.addEventListener('pointerup', stop, { once: true });
  });
}

window.addEventListener('resize', () => { resize(); fitGraph(); }); resize();
document.querySelectorAll('.moveable').forEach(makeMoveable);
Promise.all([refreshState(), refreshGraph()]).then(() => { draw(); connectEvents(); input.focus(); });
