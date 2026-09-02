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
    const radius = node === selected ? 8 : node.type === 'folder' ? Math.min(7, 3.4 + Math.sqrt(degree) * .38) : Math.min(5.2, 1.35 + Math.sqrt(degree) * .58);
    ctx.fillStyle = colors[node.type] || '#9aa8b2';
    ctx.shadowColor = ctx.fillStyle; ctx.shadowBlur = node === selected ? 18 : node.type === 'folder' ? 8 : 2;
    ctx.beginPath(); ctx.arc(node.x, node.y, radius, 0, Math.PI * 2); ctx.fill();
    ctx.shadowBlur = 0;
    if (node === selected || (node.type === 'folder' && degree >= 12 && transform.scale > .48) || (degree >= 12 && transform.scale > 1.15)) {
      ctx.fillStyle = '#dce8ee'; ctx.font = `${Math.max(9, 11 / transform.scale)}px Segoe UI`;
      ctx.fillText(node.label.slice(0, 34), node.x + radius + 4, node.y + 3);
    }
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
  selected = draggingNode || selected;
  panning = !draggingNode;
  canvas.setPointerCapture(event.pointerId);
});
canvas.addEventListener('pointermove', event => {
  if (draggingNode) { const p = graphPoint(event); draggingNode.x = p.x; draggingNode.y = p.y; draggingNode.vx = 0; draggingNode.vy = 0; }
  else if (panning) { transform.x += event.clientX - pointer.x; transform.y += event.clientY - pointer.y; }
  pointer = { x: event.clientX, y: event.clientY };
});
canvas.addEventListener('pointerup', () => { draggingNode = null; panning = false; });
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

async function submitCommand(text) {
  addMessage(text, 'user'); addMessage('MARLIN is thinking…', 'thinking', 'thinking');
  try {
    const result = await api('/api/commands', { method: 'POST', body: JSON.stringify({ text, source: 'ui' }) });
    document.getElementById('thinking')?.remove();
    if (result.message) addMessage(result.message, 'assistant');
    if (result.pending) showPending(result.pending);
    await handleClientAction(result.client_action);
    refreshState();
  } catch (error) { document.getElementById('thinking')?.remove(); addMessage(error.message, 'assistant'); }
}

document.getElementById('commandForm').addEventListener('submit', event => {
  event.preventDefault(); const text = input.value.trim(); if (!text) return; input.value = ''; submitCommand(text);
});
document.getElementById('approveAction').addEventListener('click', async () => {
  if (!pendingAction) return; const id = pendingAction.id; hidePending();
  const result = await api(`/api/actions/${id}/approve`, { method: 'POST' }); addMessage(result.message); await handleClientAction(result.client_action); refreshState();
});
document.getElementById('cancelAction').addEventListener('click', async () => {
  if (pendingAction) await api(`/api/actions/${pendingAction.id}/cancel`, { method: 'POST' }); hidePending(); addMessage('Action cancelled.');
});
document.getElementById('stopVoice').addEventListener('click', async () => { const result = await api('/api/voice/stop', { method: 'POST' }); addMessage(result.message); });
document.getElementById('listen').addEventListener('click', async event => {
  const button = event.currentTarget;
  button.textContent = 'Listening…'; button.disabled = true;
  try {
    const result = await api('/api/voice/listen', { method: 'POST' });
    if (result.text) await submitCommand(result.text);
    else if (result.error) addMessage(result.error);
  }
  catch (error) { addMessage(error.message); }
  finally { button.textContent = 'Listen'; button.disabled = false; }
});
document.getElementById('microphone').addEventListener('change', async event => {
  localStorage.setItem('marlin-microphone', event.target.value);
  await api('/api/voice/device', { method: 'POST', body: JSON.stringify({ device: event.target.value }) });
});

async function openCamera() {
  const panel = document.getElementById('cameraPanel');
  try { cameraStream = await navigator.mediaDevices.getUserMedia({ video: true, audio: false }); document.getElementById('cameraVideo').srcObject = cameraStream; panel.classList.add('active'); }
  catch { addMessage('Camera permission was not granted.'); }
}
function closeCamera() { cameraStream?.getTracks().forEach(track => track.stop()); cameraStream = null; document.getElementById('cameraPanel').classList.remove('active'); }
async function handleClientAction(action) { if (action === 'open_camera') await openCamera(); if (action === 'close_camera') closeCamera(); }
document.getElementById('openCamera').addEventListener('click', openCamera);
document.getElementById('closeCamera').addEventListener('click', closeCamera);

async function refreshState() {
  const state = await api('/api/state');
  document.getElementById('statusGrid').innerHTML = [
    ['state', state.state], ['model', state.model.loaded ? state.model.model : state.model.available ? 'model missing' : 'offline'],
    ['last reply', state.model.tokens ? `${state.model.tokens} tokens · ${state.model.total_ms} ms` : 'waiting'],
    ['prolog', state.prolog.available ? 'ready' : 'offline'], ['voice', `${state.voice.stt} / ${state.voice.tts}`],
    ['index', state.index_progress?.indexed ? `${state.index_progress.indexed} files` : state.index], ['brain', `${state.entities} nodes · ${state.relationships} links`]
  ].map(([key, value]) => `<div class="status-row"><strong>${key}</strong><span class="state-${state.state}">${value}</span></div>`).join('');
  const items = [...state.alarms.slice(0, 3).map(item => `Alarm: ${item.label}`), ...state.reminders.slice(0, 3).map(item => `Reminder: ${item.text}`)];
  document.getElementById('routineContent').textContent = items.join(' · ') || 'No alarms or reminders.';
  const voiceState = document.getElementById('voiceState');
  if (state.voice.wake_word && voiceState.textContent === 'Voice ready') voiceState.textContent = 'Say “Hey MARLIN”';
  const select = document.getElementById('microphone');
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
    if (packet.type === 'assistant.done') { document.getElementById('thinking')?.remove(); document.getElementById('streaming')?.remove(); streamingText = ''; }
    if (packet.type === 'action.preview') showPending(packet.data.action);
    if (packet.type === 'tool.call') addMessage(`Tool: ${packet.data.name}`, 'thinking');
    if (packet.type === 'prolog.result') addMessage(`Prolog: ${packet.data.query}`, 'thinking');
    if (packet.type === 'graph.refresh') refreshGraph();
    if (packet.type === 'voice.state') document.getElementById('voiceState').textContent = `Voice ${packet.data.state}`;
    if (packet.type === 'voice.level') document.getElementById('voiceLevel').style.width = `${Math.round((packet.data.level || 0) * 100)}%`;
    if (packet.type === 'wake.ready') document.getElementById('voiceState').textContent = 'Say “Hey MARLIN”';
    if (packet.type === 'wake.detected') addMessage('Yes, sir? Listening…', 'thinking', 'wake-listening');
    if (packet.type === 'wake.heard') { document.getElementById('wake-listening')?.remove(); addMessage(packet.data.text, 'user'); }
    if (packet.type === 'wake.error') { document.getElementById('wake-listening')?.remove(); addMessage(packet.data.error, 'assistant'); }
    if (packet.type === 'wake.result') {
      document.getElementById('wake-listening')?.remove();
      if (packet.data.message) addMessage(packet.data.message, 'assistant');
      if (packet.data.pending) showPending(packet.data.pending);
      handleClientAction(packet.data.client_action);
    }
    if (['assistant.state','voice.state','wake.detected','wake.result','index.progress','alarm.created','alarm.fired','reminder.created'].includes(packet.type)) refreshState();
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

window.addEventListener('resize', resize); resize();
document.querySelectorAll('.moveable').forEach(makeMoveable);
Promise.all([refreshState(), refreshGraph()]).then(() => { draw(); connectEvents(); input.focus(); });
