/**
 * PostPro Suite — Frontend Logic (PHP/SSE Version)
 * Statt WebSocket nutzen wir Server-Sent Events fuer Live-Output.
 */

const ICONS = {
  'arrow-down-circle': '\u2B07',
  'folder-open': '\uD83D\uDCC2',
  'folder': '\uD83D\uDCC1',
  'check-square': '\uD83C\uDFAB',
  'cloud-upload': '\u2601',
  'trash-2': '\uD83D\uDDD1',
};

let activeRollout = null;
let isRunning = false;
let eventSource = null;
let lineCount = 0;
let currentAbortController = null;
let currentWorkflowId = null;

// ═══ BOOT ═══
document.addEventListener('DOMContentLoaded', () => {
  setupNav();
  setupLog();
  initSettingsPassword();
  setupAdminAuth();
  loadHistory();
  if (IS_ADMIN) loadAdminData();
});

// ═══ NAVIGATION ═══
function setupNav() {
  document.querySelectorAll('.nav-item[data-view]').forEach(item => {
    item.addEventListener('click', (e) => {
      e.preventDefault();

      // Settings access check
      if (item.dataset.view === 'settings' && !IS_ADMIN && !sessionStorage.getItem('settingsUnlocked')) {
        e.preventDefault();
        document.getElementById('settingsLockOverlay').style.display = 'flex';
        return;
      }

      document.querySelectorAll('.nav-item[data-view]').forEach(n => n.classList.remove('active'));
      item.classList.add('active');
      document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
      document.getElementById('view-' + item.dataset.view).classList.add('active');

      // Lazy load
      if (item.dataset.view === 'history') loadHistory();
    });
  });
}

// ═══ SETTINGS PASSWORD PROTECTION ═══
function initSettingsPassword() {
  const form = document.getElementById('settingsPasswordForm');
  if (!form) return;

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const password = document.getElementById('settingsPassword').value;
    const statusEl = document.getElementById('settingsPasswordStatus');

    // Password validation (PostProSuite2026)
    if (password === 'PostProSuite2026!') {
      sessionStorage.setItem('settingsUnlocked', 'true');
      document.getElementById('settingsLockOverlay').style.display = 'none';
      document.getElementById('settingsContent').classList.remove('hidden');
      statusEl.textContent = '';
      form.reset();
    } else {
      statusEl.classList.add('error');
      statusEl.textContent = '❌ Falsches Passwort';
      setTimeout(() => {
        statusEl.classList.remove('error');
        statusEl.textContent = '';
      }, 2000);
    }
  });
}

// ═══ LOG PANEL ═══
function setupLog() {
  // Log panel is now a simple output area — no buttons needed
}

function addLog(text, color = 'normal') {
  const log = document.getElementById('logContent');
  const line = document.createElement('div');
  line.className = 'log-line ' + color;
  line.textContent = text;
  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
}

function copyLogContent(id) {
  const logContent = document.getElementById('logContent');
  const allLines = logContent.querySelectorAll('.log-line');
  const text = Array.from(allLines).map(line => line.textContent).join('\n');

  navigator.clipboard.writeText(text).then(() => {
    const btn = document.getElementById('copybtn-' + id);
    const originalText = btn.textContent;
    btn.textContent = '✓ Kopiert!';
    setTimeout(() => {
      btn.textContent = originalText;
    }, 2000);
  }).catch(err => {
    alert('Fehler beim Kopieren: ' + err);
  });
}

// ═══ CARD CLICK ═══
// Alle Workflows: Rollout aufklappen, Start via Button/Enter.
function onCardClick(id) {
  if (isRunning) return;

  if (activeRollout === id) { closeRollout(id); return; }
  if (activeRollout !== null) closeRollout(activeRollout);

  document.getElementById('rollout-' + id).classList.add('active');
  activeRollout = id;

  const input = document.getElementById('input-' + id);
  if (input) setTimeout(() => input.focus(), 150);
}

// Start fuer Workflows ohne Input-Feld (direkt aus Rollout)
function startWorkflow(id) {
  if (isRunning) return;
  runWorkflow(id, null);
}

function closeRollout(id) {
  document.getElementById('rollout-' + id).classList.remove('active');
  const card = document.getElementById('card-' + id);
  card.classList.remove('running');

  // Reset rollout sub-elements
  const rprogress = document.getElementById('rprogress-' + id);
  if (rprogress) { rprogress.classList.remove('active'); rprogress.textContent = ''; }

  const rfooter = document.getElementById('rfooter-' + id);
  if (rfooter) { rfooter.classList.remove('active'); }

  const rresult = document.getElementById('rresult-' + id);
  if (rresult) rresult.classList.remove('active');

  const rresultText = document.getElementById('rresult-text-' + id);
  if (rresultText) { rresultText.textContent = ''; rresultText.style.color = ''; }

  const rresultCopy = document.getElementById('rresult-copy-' + id);
  if (rresultCopy) rresultCopy.classList.add('hidden');

  const copyBtn = document.getElementById('copybtn-' + id);
  if (copyBtn) copyBtn.classList.add('hidden');

  // Remove card error banner
  const cardError = document.getElementById('card-error-' + id);
  if (cardError) cardError.remove();

  const pbar = document.getElementById('pbar-' + id);
  const pfill = document.getElementById('pfill-' + id);
  const pct = document.getElementById('percent-' + id);
  if (pbar) pbar.classList.remove('active');
  if (pfill) pfill.style.width = '0%';
  if (pct) { pct.classList.remove('active'); pct.textContent = '0%'; }

  const input = document.getElementById('input-' + id);
  if (input) input.value = '';

  if (activeRollout === id) activeRollout = null;
}

// ═══ INPUT CONFIRM ═══
function confirmInput(id) {
  const input = document.getElementById('input-' + id);
  const value = input.value.trim();
  if (!value) {
    input.classList.add('error');
    setTimeout(() => input.classList.remove('error'), 600);
    return;
  }
  runWorkflow(id, value);
}

// ═══ RUN WORKFLOW (SSE) ═══
function runWorkflow(id, inputValue) {
  isRunning = true;
  lineCount = 0;
  currentWorkflowId = id;
  currentAbortController = new AbortController();

  const wf = WORKFLOWS.find(w => w.id === id);
  setStatus(wf.title + ' laeuft ...', 'orange', 'running');
  setCardsDisabled(true, id);

  const card = document.getElementById('card-' + id);
  card.classList.add('running');
  card.classList.remove('disabled');

  const pbar = document.getElementById('pbar-' + id);
  const pfill = document.getElementById('pfill-' + id);
  const pct = document.getElementById('percent-' + id);
  pbar.classList.add('active');
  pct.classList.add('active');

  const progress = document.getElementById('rprogress-' + id);
  progress.textContent = '\u23F3  Laeuft ...';
  progress.classList.add('active');

  // Starten-Button → Stop-Button
  const startBtn = document.getElementById('startbtn-' + id);
  if (startBtn) {
    startBtn.textContent = 'Stop';
    startBtn.classList.add('stop-active');
    startBtn.onclick = function(e) { e.stopPropagation(); stopWorkflow(id); };
  }

  // Clear previous log content
  document.getElementById('logContent').innerHTML = '';

  // Clear previous error from card
  const oldError = document.getElementById('card-error-' + id);
  if (oldError) oldError.remove();

  // SSE via fetch + ReadableStream (fuer POST mit Body)
  fetch('/postpro/api/run-workflow.php', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ workflow_id: id, input_value: inputValue }),
    signal: currentAbortController.signal,
  }).then(response => {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    function processChunk() {
      reader.read().then(({ done, value }) => {
        if (done) {
          // Stream ended without done event
          if (isRunning) finishWorkflow(id, 1, 'error');
          return;
        }

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop(); // Keep incomplete line

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          try {
            const msg = JSON.parse(line.slice(6));
            handleMessage(id, msg, pfill, pct, progress);
          } catch (e) { /* ignore parse errors */ }
        }

        processChunk();
      });
    }

    processChunk();
  }).catch(err => {
    if (err.name === 'AbortError') {
      addLog('Workflow abgebrochen', 'orange');
      finishWorkflow(id, 1, 'aborted');
    } else {
      addLog('Verbindungsfehler: ' + err.message, 'red');
      finishWorkflow(id, 1, 'error');
    }
  });
}

function stopWorkflow(id) {
  if (currentAbortController && currentWorkflowId === id) {
    currentAbortController.abort();
    addLog('Beende Workflow...', 'orange');
  }
}

function handleMessage(id, msg, pfill, pct, progress) {
  switch (msg.type) {
    case 'log':
      lineCount++;
      addLog(msg.text, msg.color);
      // Linear-ish Kurve bis max 95% – kein logarithmisches Einfrieren mehr.
      // Nimmt 50 Output-Zeilen als "voll" an und skaliert linear dorthin.
      const TARGET_LINES = 50;
      const estimatedPct = Math.min(95, Math.round((lineCount / TARGET_LINES) * 95));
      pfill.style.width = estimatedPct + '%';
      pct.textContent = estimatedPct + '%';
      break;

    case 'progress':
      progress.textContent = msg.text;
      break;

    case 'clipping':
      const result = document.getElementById('rresult-' + id);
      if (msg.status === 'complete') {
        result.textContent = 'Clippings complete  \u2713';
        result.style.color = 'var(--green)';
      } else {
        result.textContent = 'Clippings missing:\n' + msg.skus.split(',').map(s => '   ' + s.trim()).join('\n');
        result.style.color = 'var(--orange)';
      }
      result.classList.add('active');
      break;

    case 'lightroom_ready':
      showLightroomSyncPrompt(id);
      break;

    case 'done':
      finishWorkflow(id, msg.code, msg.status);
      break;
  }
}

function finishWorkflow(id, code, status) {
  isRunning = false;
  currentWorkflowId = null;
  currentAbortController = null;

  // Stop-Button zurueck zu Starten
  const startBtn = document.getElementById('startbtn-' + id);
  if (startBtn) {
    const wf = WORKFLOWS.find(w => w.id === id);
    startBtn.textContent = 'Starten';
    startBtn.classList.remove('stop-active');
    if (wf && wf.input_label) {
      startBtn.onclick = function() { confirmInput(id); };
    } else {
      startBtn.onclick = function() { startWorkflow(id); };
    }
  }

  const card = document.getElementById('card-' + id);
  const pfill = document.getElementById('pfill-' + id);
  const pct = document.getElementById('percent-' + id);
  const pbar = document.getElementById('pbar-' + id);

  if (status !== 'aborted') {
    pfill.style.width = '100%';
    pct.textContent = '100%';
  }

  setTimeout(() => {
    pbar.classList.remove('active');
    pct.classList.remove('active');
    pfill.style.width = '0%';
    card.classList.remove('running');
  }, 1200);

  document.getElementById('rprogress-' + id).classList.remove('active');
  setCardsDisabled(false);
  document.getElementById('rfooter-' + id).classList.add('active');

  // Show copy button when workflow finishes
  const copyBtn = document.getElementById('copybtn-' + id);
  if (copyBtn) copyBtn.classList.remove('hidden');

  if (status === 'success' || code === 0) {
    setStatus('Bereit \u2705', 'green', '');
  } else {
    setStatus('Fehler \u274C', 'red', 'error');

    // Collect error lines from log
    const logLines = document.getElementById('logContent').querySelectorAll('.log-line.red');
    const errorText = logLines.length > 0
      ? Array.from(logLines).map(l => l.textContent).join('\n')
      : 'Fehler bei Ausfuehrung (Code ' + code + ')';

    // Show error in rollout result
    const errTextEl = document.getElementById('rresult-text-' + id);
    const errCopyBtn = document.getElementById('rresult-copy-' + id);
    const errResult = document.getElementById('rresult-' + id);
    if (errTextEl) {
      errTextEl.textContent = errorText;
      errTextEl.style.color = 'var(--red)';
    }
    if (errCopyBtn) errCopyBtn.classList.remove('hidden');
    errResult.classList.add('active');

    // Also inject error banner into the card itself
    const card = document.getElementById('card-' + id);
    const existingErr = document.getElementById('card-error-' + id);
    if (!existingErr) {
      const errDiv = document.createElement('div');
      errDiv.id = 'card-error-' + id;
      errDiv.className = 'card-error';
      errDiv.innerHTML = `
        <span class="card-error-text">${escapeHtml(errorText)}</span>
        <button class="card-error-copy" onclick="event.stopPropagation(); copyToClipboard(document.getElementById('card-error-${id}').querySelector('.card-error-text').textContent)" title="Fehler kopieren">&#128203;</button>
      `;
      card.appendChild(errDiv);
    }
  }
}

// ═══ CLIPBOARD HELPERS ═══
function copyToClipboard(text) {
  navigator.clipboard.writeText(text).then(() => {
    showCopyToast('Kopiert!');
  }).catch(() => {
    // Fallback
    const ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    showCopyToast('Kopiert!');
  });
}

function showCopyToast(msg) {
  let toast = document.getElementById('copyToast');
  if (!toast) {
    toast = document.createElement('div');
    toast.id = 'copyToast';
    toast.className = 'copy-toast';
    document.body.appendChild(toast);
  }
  toast.textContent = msg;
  toast.classList.add('visible');
  setTimeout(() => toast.classList.remove('visible'), 1500);
}

function copyError(id) {
  const textEl = document.getElementById('rresult-text-' + id);
  if (textEl) copyToClipboard(textEl.textContent);
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

// ═══ LIGHTROOM SYNC PROMPT (inline, statt osascript Popup) ═══
function showLightroomSyncPrompt(workflowId) {
  // Vermeide Duplikate
  if (document.getElementById('lightroom-prompt')) return;

  const prompt = document.createElement('div');
  prompt.id = 'lightroom-prompt';
  prompt.className = 'lightroom-prompt';
  prompt.innerHTML = `
    <div class="lr-prompt-inner">
      <div class="lr-prompt-text">
        <strong>Lightroom Sync</strong>
        <span>Input-Ordner in Lightroom synchronisieren?</span>
      </div>
      <div class="lr-prompt-actions">
        <button class="lr-btn lr-btn-cancel" onclick="dismissLightroomPrompt()">Abbrechen</button>
        <button class="lr-btn lr-btn-ok" onclick="triggerLightroomSync()">Sync starten</button>
      </div>
    </div>
  `;
  document.body.appendChild(prompt);
  requestAnimationFrame(() => prompt.classList.add('visible'));
}

function dismissLightroomPrompt() {
  const prompt = document.getElementById('lightroom-prompt');
  if (!prompt) return;
  prompt.classList.remove('visible');
  setTimeout(() => prompt.remove(), 300);
}

async function triggerLightroomSync() {
  const prompt = document.getElementById('lightroom-prompt');
  if (prompt) {
    const okBtn = prompt.querySelector('.lr-btn-ok');
    if (okBtn) {
      okBtn.disabled = true;
      okBtn.textContent = 'Oeffne Lightroom ...';
    }
  }
  addLog('Starte Lightroom Sync ...', 'orange');
  try {
    const res = await fetch('/postpro/api/lightroom-sync.php', { method: 'POST' });
    const data = await res.json();
    if (data.status === 'ok') {
      addLog('Lightroom Sync abgeschlossen', 'green');
    } else {
      addLog('Lightroom Sync Fehler: ' + (data.output || 'unbekannt'), 'red');
    }
  } catch (e) {
    addLog('Lightroom Sync Fehler: ' + e.message, 'red');
  } finally {
    dismissLightroomPrompt();
  }
}

// ═══ UI HELPERS ═══
function setStatus(text, color, barClass) {
  const el = document.getElementById('statusText');
  const bar = document.querySelector('.status-bar');
  el.textContent = text;
  el.style.color = 'var(--' + color + ')';
  bar.className = 'status-bar' + (barClass ? ' ' + barClass : '');
}

function setCardsDisabled(disabled, exceptId) {
  document.querySelectorAll('.card').forEach(c => {
    const cid = parseInt(c.id.replace('card-', ''));
    if (disabled && cid !== exceptId) {
      c.classList.add('disabled');
    } else {
      c.classList.remove('disabled');
    }
  });
}

// ═══ EXPORTS ═══
async function loadHistory() {
  try {
    const resExports = await fetch('/postpro/api/exports.php');
    const exports = await resExports.json();
    const el = document.getElementById('historyList');

    let html = '';

    // Exports section only
    if (exports && exports.length > 0) {
      html = exports.slice(0, 50).map(exp => `
        <div class="admin-row">
          <div class="admin-info">
            <div class="admin-title">${exp.ticket_key} <span class="export-size">${exp.size_formatted}</span></div>
            <div class="admin-sub">${exp.created}</div>
          </div>
          <a href="${exp.download_url}" class="download-btn" download>⬇ Download</a>
        </div>
      `).join('');
    } else {
      html = '<div class="update-empty">Noch keine Exporte vorhanden.</div>';
    }

    el.innerHTML = html;
  } catch (e) {
    console.error('loadExports error:', e);
  }
}

// ═══ UPDATES ═══
async function pollUpdates() {
  try {
    const res = await fetch('/postpro/api/updates.php');
    const data = await res.json();
    updateBadge(data.unseen_count);
    renderUpdates(data.updates);
  } catch (e) {}
  setTimeout(pollUpdates, 60000);
}

function updateBadge(count) {
  const badge = document.getElementById('updateBadge');
  if (!badge) return;
  if (count > 0) {
    badge.textContent = count;
    badge.style.display = 'inline-block';
  } else {
    badge.style.display = 'none';
  }
}

function renderUpdates(updates) {
  const list = document.getElementById('updatesList');
  if (!list) return;
  if (!updates || updates.length === 0) {
    list.innerHTML = '<div class="update-empty">Keine Updates vorhanden.</div>';
    return;
  }
  list.innerHTML = updates.map(u => `
    <div class="update-item" onclick="markUpdateSeen(${u.id})">
      <div class="update-header">
        <span class="update-version">V${u.version}</span>
        <span class="update-date">${new Date(u.created_at).toLocaleDateString('de-DE')}</span>
      </div>
      <div class="update-msg">${u.message}</div>
    </div>
  `).join('');
}

async function markUpdateSeen(id) {
  try {
    await fetch('/postpro/api/updates.php?action=seen&id=' + id, { method: 'POST' });
    pollUpdates();
  } catch (e) {}
}

async function createUpdate() {
  const version = document.getElementById('updateVersion').value.trim();
  const message = document.getElementById('updateMessage').value.trim();
  if (!version || !message) return;

  try {
    const res = await fetch('/postpro/api/updates.php', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ version, message }),
    });
    if (res.ok) {
      document.getElementById('updateVersion').value = '';
      document.getElementById('updateMessage').value = '';
      pollUpdates();
    }
  } catch (e) {}
}

// ═══ ADMIN ═══
async function loadAdminData() {
  const [scripts, users, config, history] = await Promise.allSettled([
    fetch('/postpro/api/admin/scripts.php').then(r => r.json()),
    fetch('/postpro/api/admin/users.php').then(r => r.json()),
    fetch('/postpro/api/admin/config.php').then(r => r.json()),
    fetch('/postpro/api/admin/logs.php').then(r => r.json()),
  ]);

  if (scripts.status === 'fulfilled') renderScripts(scripts.value);
  if (users.status === 'fulfilled') renderUsers(users.value);
  if (config.status === 'fulfilled') renderConfig(config.value);
  if (history.status === 'fulfilled') renderAdminHistory(history.value);
}

function renderScripts(scripts) {
  const el = document.getElementById('scriptsList');
  if (!el || !scripts) return;
  el.innerHTML = scripts.map(s => `
    <div class="admin-row">
      <span class="admin-icon">${ICONS[s.icon] || '\u25B6'}</span>
      <div class="admin-info">
        <div class="admin-title">${s.title}</div>
        <div class="admin-sub">${s.script} \u00B7 ${s.exists ? (s.size + ' B') : 'fehlt'}</div>
      </div>
      <span class="admin-status ${s.exists ? 'ok' : 'missing'}">${s.exists ? '\u2713' : '\u2717'}</span>
    </div>
  `).join('');
}

function renderUsers(users) {
  const el = document.getElementById('usersList');
  if (!el || !users) return;
  el.innerHTML = users.map(u => `
    <div class="admin-row">
      <div class="admin-info">
        <div class="admin-title">${u.name}</div>
        <div class="admin-sub">${u.email}</div>
      </div>
      <select class="role-select" onchange="updateUserRole(${u.id}, this.value)">
        <option value="user" ${u.role === 'user' ? 'selected' : ''}>User</option>
        <option value="admin" ${u.role === 'admin' ? 'selected' : ''}>Admin</option>
      </select>
    </div>
  `).join('');
}

async function updateUserRole(userId, role) {
  try {
    await fetch('/postpro/api/admin/users.php?id=' + userId, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ role }),
    });
  } catch (e) {}
}

function renderConfig(config) {
  const el = document.getElementById('configList');
  if (!el || !Array.isArray(config)) return;
  el.innerHTML = config.map(c => `
    <div class="admin-row config-row">
      <div class="config-key">${c.key}</div>
      <input class="config-val ${c.masked ? 'masked' : ''}"
             value="${c.value}" data-key="${c.key}" onchange="markConfigDirty()">
    </div>
  `).join('');

  el.insertAdjacentHTML('beforeend', `
    <button class="rollout-start" id="configSaveBtn" style="margin-top:12px;display:none" onclick="saveConfig()">
      Speichern
    </button>
  `);
}

function markConfigDirty() {
  const btn = document.getElementById('configSaveBtn');
  if (btn) btn.style.display = 'block';
}

async function saveConfig() {
  const inputs = document.querySelectorAll('.config-val');
  const items = [];
  inputs.forEach(input => {
    items.push({ key: input.dataset.key, value: input.value, masked: input.classList.contains('masked') });
  });
  try {
    const res = await fetch('/postpro/api/admin/config.php', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(items),
    });
    if (res.ok) {
      const btn = document.getElementById('configSaveBtn');
      if (btn) { btn.textContent = 'Gespeichert \u2713'; btn.style.background = 'var(--green)'; }
    }
  } catch (e) {}
}

function renderAdminHistory(history) {
  const el = document.getElementById('adminHistory');
  if (!el || !history) return;
  if (history.length === 0) {
    el.innerHTML = '<div class="update-empty">Noch keine Runs.</div>';
    return;
  }
  el.innerHTML = history.slice(0, 50).map(h => `
    <div class="admin-row">
      <div class="admin-info">
        <div class="admin-title">${h.title}</div>
        <div class="admin-sub">${h.user_email} \u00B7 ${new Date(h.started_at).toLocaleString('de-DE')}</div>
      </div>
      <span class="admin-status ${h.status === 'done' ? 'ok' : 'missing'}">${h.status}</span>
    </div>
  `).join('');
}

// ═══ BACKEND ADMIN AUTHENTICATION ═══
let adminPassword = '';

function setupAdminAuth() {
  checkBackendAuth();
  const form = document.getElementById('adminAuthForm');
  if (form) {
    form.addEventListener('submit', adminAuthHandler);
  }
}

async function checkBackendAuth() {
  try {
    const res = await fetch('/postpro/api/admin-auth.php', { method: 'GET' });
    const data = await res.json();
    if (data.authenticated) {
      showBackendPanel();
      loadBackendScripts();
    } else {
      hideBackendPanel();
    }
  } catch (e) {
    hideBackendPanel();
  }
}

async function adminAuthHandler(e) {
  e.preventDefault();
  const password = document.getElementById('adminPassword').value;
  const statusEl = document.getElementById('adminAuthStatus');

  if (!password) {
    statusEl.textContent = 'Passwort erforderlich';
    statusEl.style.color = 'var(--red)';
    return;
  }

  try {
    const res = await fetch('/postpro/api/admin-auth.php', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: 'password=' + encodeURIComponent(password),
    });
    const data = await res.json();

    if (res.ok) {
      statusEl.textContent = 'Angemeldet \u2713';
      statusEl.style.color = 'var(--green)';
      adminPassword = password; // Store password for subsequent requests
      document.getElementById('adminPassword').value = '';
      showBackendPanel();
      loadBackendScripts();
    } else {
      statusEl.textContent = 'Falsches Passwort';
      statusEl.style.color = 'var(--red)';
    }
  } catch (e) {
    statusEl.textContent = 'Fehler: ' + e.message;
    statusEl.style.color = 'var(--red)';
  }
}

function showBackendPanel() {
  document.getElementById('backendAccess').classList.add('hidden');
  document.getElementById('scriptEditorPanel').classList.remove('hidden');
}

function hideBackendPanel() {
  document.getElementById('backendAccess').classList.remove('hidden');
  document.getElementById('scriptEditorPanel').classList.add('hidden');
  document.getElementById('scriptEditorModal').classList.add('hidden');
}

async function logoutBackend() {
  adminPassword = '';
  hideBackendPanel();
  document.getElementById('adminPassword').value = '';
  document.getElementById('adminAuthStatus').textContent = '';
}

async function loadBackendScripts() {
  try {
    const res = await fetch('/postpro/api/admin/edit-script.php?action=list&password=' + encodeURIComponent(adminPassword));
    const scripts = await res.json();
    const listEl = document.getElementById('scriptsList');
    if (!scripts || !Array.isArray(scripts) || scripts.length === 0) {
      listEl.innerHTML = '<div class="update-empty">Keine Skripte gefunden.</div>';
      return;
    }
    listEl.innerHTML = scripts.map(s => `
      <div class="script-item" onclick="openScriptEditor('${s.name}')">
        <div class="script-name">${s.name}</div>
        <div class="script-meta">${s.size} B \u00B7 ${new Date(s.modified * 1000).toLocaleString('de-DE')}</div>
      </div>
    `).join('');
  } catch (e) {
    const listEl = document.getElementById('scriptsList');
    listEl.innerHTML = '<div class="update-empty">Fehler beim Laden: ' + e.message + '</div>';
  }
}

async function openScriptEditor(scriptName) {
  try {
    const res = await fetch('/postpro/api/admin/edit-script.php?action=read&script=' + encodeURIComponent(scriptName) + '&password=' + encodeURIComponent(adminPassword));
    const data = await res.json();
    if (data.error) {
      alert('Fehler: ' + data.error);
      return;
    }
    document.getElementById('editorTitle').textContent = 'Bearbeite: ' + scriptName;
    document.getElementById('scriptContent').value = data.content;
    document.getElementById('scriptContent').dataset.scriptName = scriptName;
    document.getElementById('scriptEditorModal').classList.remove('hidden');
  } catch (e) {
    alert('Fehler beim Laden: ' + e.message);
  }
}

function closeScriptEditor() {
  document.getElementById('scriptEditorModal').classList.add('hidden');
  document.getElementById('scriptContent').value = '';
  document.getElementById('scriptContent').dataset.scriptName = '';
}

async function saveScript() {
  const scriptName = document.getElementById('scriptContent').dataset.scriptName;
  const content = document.getElementById('scriptContent').value;

  if (!scriptName) {
    alert('Skriptname nicht definiert');
    return;
  }

  try {
    const res = await fetch('/postpro/api/admin/edit-script.php', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: 'action=save&script=' + encodeURIComponent(scriptName) + '&content=' + encodeURIComponent(content) + '&password=' + encodeURIComponent(adminPassword),
    });
    const data = await res.json();

    if (res.ok) {
      alert('Skript gespeichert! Backup erstellt.');
      closeScriptEditor();
      loadBackendScripts();
    } else {
      alert('Fehler beim Speichern: ' + (data.error || 'Unbekannter Fehler'));
    }
  } catch (e) {
    alert('Fehler: ' + e.message);
  }
}
