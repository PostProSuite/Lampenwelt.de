const express = require('express');
const cors = require('cors');
const path = require('path');
const http = require('http');
const session = require('express-session');
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const CustomUpdater = require('./lib/customUpdater');
const { resolveScriptPath, getUserScriptsDir, checkScriptUpdatesAvailable, checkAndUpdateScripts } = require('./script-updater');
const app = express();

// In the packaged .app, Python scripts are unpacked outside the .asar archive
const SCRIPTS_DIR = __dirname.includes('app.asar')
  ? path.join(__dirname.replace('app.asar', 'app.asar.unpacked'), 'src', 'scripts')
  : path.join(__dirname, 'src', 'scripts');

// User-override dir for delta-updated scripts
// Python will look here FIRST, then fall back to SCRIPTS_DIR
const USER_SCRIPTS_DIR = getUserScriptsDir();

// Load config.env for Python scripts
// In packaged app, files are in app.asar.unpacked
const CONFIG_PATH = __dirname.includes('app.asar')
  ? path.join(__dirname.replace('app.asar', 'app.asar.unpacked'), 'src', 'scripts', 'config', 'config.env')
  : path.join(__dirname, 'src', 'scripts', 'config', 'config.env');
let pythonEnvVars = { ...process.env };

if (fs.existsSync(CONFIG_PATH)) {
  try {
    const configContent = fs.readFileSync(CONFIG_PATH, 'utf8');
    const lines = configContent.split('\n');
    for (const line of lines) {
      const trimmed = line.trim();
      if (trimmed && !trimmed.startsWith('#')) {
        const match = trimmed.match(/^([^=]+)=(.*)$/);
        if (match) {
          pythonEnvVars[match[1]] = match[2];
        }
      }
    }
  } catch (err) {
    console.warn('⚠ Fehler beim Laden von config.env:', err.message);
  }
}

// ═══ WORKSPACE INIT ═══
// 01-Input RAW files: FLAT (keine Unterordner — Download legt Bilder direkt rein)
// 02-Webcheck: drei Lightroom-Export-Unterordner (Mainimage / Mood / Pos4-X)
const WEBCHECK_SUBFOLDERS = [
  '01-Mainimage',
  '02-Mood',
  '03-Pos4-X',
];

const WORKSPACE_FOLDERS = [
  '01-Input RAW files',
  '02-Webcheck',
  ...WEBCHECK_SUBFOLDERS.map(s => `02-Webcheck/${s}`),
  '03-Upload',
  'Exports',
  'logs',
];

function resolveWorkspace() {
  const raw = (pythonEnvVars['POSTPRO_WORKSPACE'] || '').trim();
  return raw.replace(/^~/, os.homedir());
}

function initWorkspace() {
  const workspace = resolveWorkspace();
  if (!workspace) return { workspace: null, created: [], status: 'no_config' };

  const created = [];
  const all = [workspace, ...WORKSPACE_FOLDERS.map(f => path.join(workspace, f))];

  for (const dir of all) {
    if (!fs.existsSync(dir)) {
      fs.mkdirSync(dir, { recursive: true });
      created.push(dir);
      console.log('✓ Ordner erstellt:', dir);
    }
  }

  if (created.length > 0) {
    console.log(`✓ Workspace eingerichtet: ${workspace}`);
  } else {
    console.log('✓ Workspace OK:', workspace);
  }

  return { workspace, created, status: created.length > 0 ? 'created' : 'ok' };
}

// ═══ CLEANUP VOR DOWNLOAD RAW ═══
// Wird vor jedem Download-RAW-Workflow (ID 0 = SKU, ID 1 = Category) aufgerufen.
// Das ist der EINZIGE Cleanup-Trigger — Jira Final und Upload bereinigen NICHTS mehr.
// So bleiben Files nach jedem Workflow zur Inspektion liegen, der naechste Download
// macht den frischen Start.
//
// Räumt drei Bereiche:
//   1) 01-Input RAW files  → komplett rekursiv leeren (Files + evtl. Subfolders;
//                            Parent-Ordner bleibt stehen)
//   2) 02-Webcheck         → in JEDEM Unterordner nur die Dateien löschen.
//                            Webcheck selbst und seine Unterordner bleiben stehen
//                            (Lightroom hat sie verlinkt — Ordner löschen würde
//                            Lightroom-Sync brechen).
//   3) 03-Upload           → komplett rekursiv leeren (Files + evtl. Subfolders;
//                            Parent-Ordner bleibt stehen)
function cleanupBeforeDownloadRaw() {
  const workspace = resolveWorkspace();
  if (!workspace) {
    return { ok: false, reason: 'no_workspace', removedInput: 0, removedWebcheck: 0, removedUpload: 0 };
  }

  let removedInput = 0;
  let removedWebcheck = 0;
  let removedUpload = 0;
  const errors = [];

  // ── 1) 01-Input RAW files: rekursiv leeren ─────────────────────
  const inputRaw = path.join(workspace, '01-Input RAW files');
  if (fs.existsSync(inputRaw)) {
    try {
      for (const entry of fs.readdirSync(inputRaw)) {
        if (entry.startsWith('.')) continue; // .DS_Store etc. lassen
        const p = path.join(inputRaw, entry);
        try {
          const stat = fs.lstatSync(p);
          if (stat.isDirectory()) {
            fs.rmSync(p, { recursive: true, force: true });
          } else {
            fs.unlinkSync(p);
          }
          removedInput++;
        } catch (err) {
          errors.push(`01-Input/${entry}: ${err.message}`);
        }
      }
    } catch (err) {
      errors.push(`01-Input read: ${err.message}`);
    }
  }

  // ── 2) 02-Webcheck: nur Dateien in Unterordnern löschen ────────
  const webcheck = path.join(workspace, '02-Webcheck');
  if (fs.existsSync(webcheck)) {
    try {
      for (const sub of fs.readdirSync(webcheck)) {
        if (sub.startsWith('.')) continue;
        const subPath = path.join(webcheck, sub);
        let stat;
        try { stat = fs.lstatSync(subPath); } catch (_) { continue; }
        if (!stat.isDirectory()) continue; // nur Ordner verarbeiten
        try {
          for (const file of fs.readdirSync(subPath)) {
            if (file.startsWith('.')) continue;
            const filePath = path.join(subPath, file);
            try {
              const fstat = fs.lstatSync(filePath);
              if (fstat.isFile()) {
                fs.unlinkSync(filePath);
                removedWebcheck++;
              }
              // Unterordner innerhalb eines Webcheck-Subfolders bleiben unangetastet
            } catch (err) {
              errors.push(`02-Webcheck/${sub}/${file}: ${err.message}`);
            }
          }
        } catch (err) {
          errors.push(`02-Webcheck/${sub} read: ${err.message}`);
        }
      }
    } catch (err) {
      errors.push(`02-Webcheck read: ${err.message}`);
    }
  }

  // ── 3) 03-Upload: rekursiv leeren ──────────────────────────────
  // Wichtig: nach erfolgreichem Upload bleiben hier die Files liegen
  // (cleanup_after_upload wurde aus 10-2 entfernt). Erst der naechste
  // Download-RAW-Lauf raeumt sie weg.
  const uploadDir = path.join(workspace, '03-Upload');
  if (fs.existsSync(uploadDir)) {
    try {
      for (const entry of fs.readdirSync(uploadDir)) {
        if (entry.startsWith('.')) continue;
        const p = path.join(uploadDir, entry);
        try {
          const stat = fs.lstatSync(p);
          if (stat.isDirectory()) {
            fs.rmSync(p, { recursive: true, force: true });
          } else {
            fs.unlinkSync(p);
          }
          removedUpload++;
        } catch (err) {
          errors.push(`03-Upload/${entry}: ${err.message}`);
        }
      }
    } catch (err) {
      errors.push(`03-Upload read: ${err.message}`);
    }
  }

  if (errors.length > 0) {
    console.warn(`⚠ Cleanup: ${errors.length} Fehler:`, errors.slice(0, 5));
  }
  console.log(`🧹 Cleanup vor Download RAW: 01-Input=${removedInput}, 02-Webcheck=${removedWebcheck}, 03-Upload=${removedUpload}`);
  return { ok: true, removedInput, removedWebcheck, removedUpload, errors };
}

// Workspace beim Start prüfen/erstellen
const workspaceInit = initWorkspace();

// Middleware
app.use(cors());
app.use(express.json());
app.use(express.urlencoded({ extended: true }));
app.use(session({
  secret: 'PostProSuite2026!',
  resave: false,
  saveUninitialized: true,
  cookie: { secure: false, maxAge: 1000 * 60 * 60 * 24 }
}));

// Statische Dateien:
// 1) User-override (Delta-Updates) zuerst - so können UI-Fixes ohne DMG-Update ausgerollt werden
// 2) Bundled public/ als Fallback
const { getUserOverrideBase, resolvePublicPath } = require('./script-updater');
const USER_PUBLIC_DIR = path.join(getUserOverrideBase(), 'public');
if (fs.existsSync(USER_PUBLIC_DIR)) {
  app.use(express.static(USER_PUBLIC_DIR));
  console.log(`✓ User-override public/ aktiv: ${USER_PUBLIC_DIR}`);
}
app.use(express.static(path.join(__dirname, 'public')));

// Helper: sendet Public-Datei (user-override bevorzugt)
function sendPublic(res, fileName) {
  res.sendFile(resolvePublicPath(__dirname, fileName));
}

// ═══ SIMPLE AUTH SIMULATION ═══
const VALID_USER = {
  email: 'admin@postpro.local',
  name: 'Admin',
  role: 'admin'
};

// ═══ ROUTES ═══

// Login Check
app.get('/api/user', (req, res) => {
  if (req.session.user) {
    res.json(req.session.user);
  } else {
    res.status(401).json({ error: 'Not authenticated' });
  }
});

// Fake Login
app.post('/api/login', (req, res) => {
  req.session.user = VALID_USER;
  res.json({ success: true, user: VALID_USER });
});

// Helper: zentraler Exports-Pfad - immer im Workspace
function getExportsPath() {
  const workspace = resolveWorkspace();
  return path.join(workspace, 'Exports');
}

// Get Exports
app.get('/api/get-exports', (req, res) => {
  try {
    const exportsPath = getExportsPath();
    if (!fs.existsSync(exportsPath)) {
      fs.mkdirSync(exportsPath, { recursive: true });
      return res.json({ exports: [], path: exportsPath });
    }

    const files = fs.readdirSync(exportsPath)
      .filter(f => !f.startsWith('.'))
      .filter(f => f.endsWith('.xlsx') || f.endsWith('.xls') || f.endsWith('.csv'))
      .map(f => {
        const fullPath = path.join(exportsPath, f);
        const stats = fs.statSync(fullPath);
        return {
          name: f,
          size: stats.size,
          sizeKB: (stats.size / 1024).toFixed(1) + ' KB',
          mtime: stats.mtime.getTime(),
          date: new Date(stats.mtime).toLocaleString('de-DE'),
          path: `/api/download-export/${encodeURIComponent(f)}`
        };
      })
      .sort((a, b) => b.mtime - a.mtime); // Newest first

    res.json({ exports: files, path: exportsPath });
  } catch (err) {
    console.error('Fehler beim Laden der Exporte:', err);
    res.status(500).json({ error: err.message });
  }
});

// Download Export
app.get('/api/download-export/:filename', (req, res) => {
  try {
    const filename = decodeURIComponent(req.params.filename);
    if (filename.includes('..') || filename.includes('/') || filename.startsWith('.')) {
      return res.status(400).json({ error: 'Invalid filename' });
    }

    const exportsPath = getExportsPath();
    const filePath = path.join(exportsPath, filename);

    if (!filePath.startsWith(exportsPath) || !fs.existsSync(filePath)) {
      return res.status(404).json({ error: 'File not found' });
    }

    res.download(filePath, filename);
  } catch (err) {
    console.error('Fehler beim Download:', err);
    res.status(500).json({ error: err.message });
  }
});

// Delete Export - löscht Datei aus Workspace/Exports
app.delete('/api/delete-export/:filename', (req, res) => {
  try {
    const filename = decodeURIComponent(req.params.filename);
    if (filename.includes('..') || filename.includes('/') || filename.startsWith('.')) {
      return res.status(400).json({ error: 'Invalid filename' });
    }

    const exportsPath = getExportsPath();
    const filePath = path.join(exportsPath, filename);

    // Sicherheits-Check: muss im Exports-Ordner liegen
    if (!filePath.startsWith(exportsPath)) {
      return res.status(400).json({ error: 'Path traversal detected' });
    }
    if (!fs.existsSync(filePath)) {
      return res.status(404).json({ error: 'File not found' });
    }

    // Tatsächlich löschen
    fs.unlinkSync(filePath);
    console.log(`✓ Export gelöscht: ${filename}`);
    res.json({ success: true, deleted: filename });
  } catch (err) {
    console.error('Fehler beim Löschen:', err);
    res.status(500).json({ error: err.message });
  }
});

// ═══ WORKFLOW EXECUTION ═══
const WORKFLOWS = [
  { id: 0, script: '00-SKU-based-json-2.py', name: 'Download RAW (SKU)' },
  { id: 1, script: '03-1_DAM-API-Request-Download.py', name: 'Download RAW (Category ID)' },
  { id: 2, script: '02-1_filenaming.py', name: 'Image Classification' },
  { id: 3, script: '04-1_Jira-Final.py', name: 'Jira Ticket Completion' },
  { id: 4, script: '10-2_Upload-DAM-Direct.py', name: 'Upload to DAM' },
];

let runningProcess = null;

// ═══ LIGHTROOM SYNC ═══
function triggerLightroomSync() {
  const delay = parseInt(pythonEnvVars['LIGHTROOM_STARTUP_DELAY'] || '8');
  const script = [
    'tell application "Adobe Lightroom Classic" to activate',
    `delay ${delay}`,
    'tell application "System Events"',
    '    tell process "Adobe Lightroom Classic"',
    '        key code 5',
    '        delay 1',
    '        click menu item "Ordner synchronisieren..." of menu "Bibliothek" of menu bar 1',
    '        delay 2',
    '        tell window 1',
    '            if exists button "Synchronisieren" then',
    '                click button "Synchronisieren"',
    '            else if exists button "Synchronize" then',
    '                click button "Synchronize"',
    '            end if',
    '        end tell',
    '    end tell',
    'end tell',
  ].join('\n');
  spawn('osascript', ['-e', script]);
}

app.post('/api/run-workflow', (req, res) => {
  const { workflow_id, input_value } = req.body;

  // Validate workflow_id
  if (workflow_id === undefined || workflow_id === null) {
    return res.status(400).json({ error: 'Workflow ID required' });
  }

  const workflow = WORKFLOWS.find(w => w.id === workflow_id);

  if (!workflow) {
    return res.status(400).json({ error: `Workflow with ID ${workflow_id} not found` });
  }

  // SSE Header
  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-cache');
  res.setHeader('Connection', 'keep-alive');

  // Build command - prefer user-override (delta-updated) over bundled
  const scriptPath = resolveScriptPath(__dirname, workflow.script);
  const pythonArgs = input_value ? [scriptPath, input_value] : [scriptPath];

  // Log workflow start
  const timestamp = new Date().toLocaleTimeString('de-DE');
  res.write(`data: ${JSON.stringify({ type: 'log', text: `[${timestamp}] ▶ Starting workflow: ${workflow.name}`, color: 'blue' })}\n\n`);

  // Check if input is required but missing
  if ((workflow_id === 0 || workflow_id === 1 || workflow_id === 3) && !input_value) {
    res.write(`data: ${JSON.stringify({ type: 'log', text: '❌ Error: This workflow requires input', color: 'red' })}\n\n`);
    res.write(`data: ${JSON.stringify({ type: 'done', code: 1, status: 'error', message: 'Missing required input' })}\n\n`);
    res.end();
    return;
  }

  // ═══ CLEANUP vor Download RAW ═══
  // Bei Workflow 0 (SKU) oder 1 (Category) den Input-Bereich für das neue Batch leeren:
  //   - 01-Input RAW files komplett (inkl. Subfolders)
  //   - 02-Webcheck nur Dateien in Unterordnern (Ordner bleiben — Lightroom-Sync!)
  if (workflow_id === 0 || workflow_id === 1) {
    const ts2 = new Date().toLocaleTimeString('de-DE');
    res.write(`data: ${JSON.stringify({ type: 'log', text: `[${ts2}] 🧹 Cleanup: 01-Input RAW + Webcheck + 03-Upload werden geleert…`, color: 'normal' })}\n\n`);
    try {
      const result = cleanupBeforeDownloadRaw();
      if (result.ok) {
        const summary = `[${new Date().toLocaleTimeString('de-DE')}] ✓ Cleanup OK — 01-Input: ${result.removedInput}, 02-Webcheck: ${result.removedWebcheck} Dateien, 03-Upload: ${result.removedUpload}`;
        res.write(`data: ${JSON.stringify({ type: 'log', text: summary, color: 'green' })}\n\n`);
        if (result.errors && result.errors.length > 0) {
          res.write(`data: ${JSON.stringify({ type: 'log', text: `⚠ ${result.errors.length} Cleanup-Fehler (siehe App-Logs)`, color: 'red' })}\n\n`);
        }
      } else {
        res.write(`data: ${JSON.stringify({ type: 'log', text: `⚠ Cleanup übersprungen: ${result.reason}`, color: 'red' })}\n\n`);
      }
    } catch (err) {
      res.write(`data: ${JSON.stringify({ type: 'log', text: `⚠ Cleanup-Fehler: ${err.message} — Workflow läuft trotzdem weiter`, color: 'red' })}\n\n`);
    }
  }

  // Start Python process with config environment variables + input value
  const spawnEnv = { ...pythonEnvVars };
  if (input_value) spawnEnv['POSTPRO_INPUT'] = input_value;

  // PYTHONPATH: user-override-dir FIRST, then bundled - so updated _utils.py wins
  // This allows delta-updating individual scripts without touching others
  const existingPythonPath = spawnEnv['PYTHONPATH'] ? `:${spawnEnv['PYTHONPATH']}` : '';
  spawnEnv['PYTHONPATH'] = `${USER_SCRIPTS_DIR}:${SCRIPTS_DIR}${existingPythonPath}`;

  // Bundled-paths als env vars, damit _utils.py Assets findet (ML-Modell, JSON)
  // auch wenn es aus dem user-override-dir geladen wurde
  spawnEnv['POSTPRO_BUNDLED_SCRIPTS'] = SCRIPTS_DIR;
  spawnEnv['POSTPRO_BUNDLED_SRC'] = path.dirname(SCRIPTS_DIR);

  const python = spawn('python3', pythonArgs, { env: spawnEnv, cwd: SCRIPTS_DIR });
  runningProcess = python;
  let hasError = false;

  // Capture output
  python.stdout.on('data', (data) => {
    const lines = data.toString().split('\n');
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      if (trimmed === '##LIGHTROOM_READY##') {
        const ts = new Date().toLocaleTimeString('de-DE');
        res.write(`data: ${JSON.stringify({ type: 'log', text: `[${ts}] ⬡ Lightroom wird geöffnet und synchronisiert…`, color: 'blue' })}\n\n`);
        triggerLightroomSync();
      } else if (trimmed.startsWith('##CLIPPING_CHECK##')) {
        // e.g. ##CLIPPING_CHECK##:missing:10050196, 10050197
        //      ##CLIPPING_CHECK##:complete
        const parts = trimmed.split(':');
        const status = parts[1]; // 'missing' or 'complete'
        const skus   = parts.slice(2).join(':').trim(); // SKU list or empty
        res.write(`data: ${JSON.stringify({ type: 'clipping_check', status, skus })}\n\n`);
      } else {
        res.write(`data: ${JSON.stringify({ type: 'log', text: trimmed, color: 'normal' })}\n\n`);
      }
    }
  });

  python.stderr.on('data', (data) => {
    const text = data.toString().trim();
    if (text) {
      hasError = true;
      res.write(`data: ${JSON.stringify({ type: 'log', text: text, color: 'red' })}\n\n`);
    }
  });

  // Handle completion
  python.on('close', (code) => {
    runningProcess = null;
    const timestamp = new Date().toLocaleTimeString('de-DE');
    if (code === 0) {
      res.write(`data: ${JSON.stringify({ type: 'log', text: `[${timestamp}] ✓ Workflow completed successfully`, color: 'green' })}\n\n`);
    } else {
      res.write(`data: ${JSON.stringify({ type: 'log', text: `[${timestamp}] ❌ Workflow failed with exit code ${code}`, color: 'red' })}\n\n`);
    }
    res.write(`data: ${JSON.stringify({ type: 'done', code: code, status: code === 0 ? 'success' : 'error' })}\n\n`);
    res.end();
  });

  // Handle errors (e.g., Python not found)
  python.on('error', (err) => {
    runningProcess = null;
    res.write(`data: ${JSON.stringify({ type: 'log', text: `❌ System Error: ${err.message}`, color: 'red' })}\n\n`);
    if (err.code === 'ENOENT') {
      res.write(`data: ${JSON.stringify({ type: 'log', text: '⚠ Python3 not found. Make sure Python3 is installed and in PATH', color: 'red' })}\n\n`);
    }
    res.write(`data: ${JSON.stringify({ type: 'done', code: 1, status: 'error', message: err.message })}\n\n`);
    res.end();
  });
});

// Workspace Status API
app.get('/api/workspace-status', (req, res) => {
  const workspace = resolveWorkspace();
  if (!workspace) return res.json({ status: 'no_config' });

  const folders = WORKSPACE_FOLDERS.map(name => ({
    name,
    path: path.join(workspace, name),
    exists: fs.existsSync(path.join(workspace, name)),
  }));

  res.json({
    status: workspaceInit.status,
    firstRun: workspaceInit.created.length > 0,
    workspace,
    folders,
  });
});

// Kill running workflow
app.post('/api/kill-workflow', (req, res) => {
  if (runningProcess) {
    runningProcess.kill('SIGTERM');
    res.json({ killed: true });
  } else {
    res.json({ killed: false, message: 'No process running' });
  }
});

// Python environment check
app.get('/api/python-check', (req, res) => {
  const check = spawn('python3', ['-c', 'import requests, paramiko, PIL, openpyxl, aiohttp, dotenv, jira']);
  let stderr = '';
  check.stderr.on('data', d => { stderr += d.toString(); });
  check.on('close', code => {
    res.json({ ok: code === 0, error: stderr.trim() || null });
  });
  check.on('error', err => {
    res.json({ ok: false, error: err.message });
  });
});

// Open folder in Finder
app.post('/api/open-folder', (req, res) => {
  const { folder } = req.body;
  const workspace = resolveWorkspace();
  if (!workspace) return res.status(400).json({ error: 'No workspace configured' });

  let target;
  if (WORKSPACE_FOLDERS.includes(folder) || folder === 'Exports') {
    target = path.join(workspace, folder);
  } else if (!folder || folder === '') {
    target = workspace;
  } else {
    return res.status(403).json({ error: 'Forbidden' });
  }

  spawn('open', [target]);
  res.json({ ok: true });
});

// Get exports list
app.get('/api/exports', (req, res) => {
  const workspace = resolveWorkspace();
  if (!workspace) return res.json({ files: [], error: 'No workspace' });

  const exportsDir = path.join(workspace, 'Exports');
  if (!fs.existsSync(exportsDir)) {
    return res.json({ files: [] });
  }

  try {
    const files = fs.readdirSync(exportsDir)
      .filter(f => f.endsWith('.xlsx') || f.endsWith('.xls') || f.endsWith('.csv'))
      .map(f => {
        const filePath = path.join(exportsDir, f);
        const stat = fs.statSync(filePath);
        return {
          name: f,
          size: stat.size,
          modified: stat.mtime.toISOString(),
          path: filePath
        };
      })
      .sort((a, b) => new Date(b.modified) - new Date(a.modified));

    res.json({ files });
  } catch (err) {
    res.json({ files: [], error: err.message });
  }
});

// Download export file
app.get('/api/exports/:filename', (req, res) => {
  const workspace = resolveWorkspace();
  if (!workspace) return res.status(400).json({ error: 'No workspace' });

  const filename = req.params.filename;
  const filePath = path.join(workspace, 'Exports', filename);

  // Security: prevent directory traversal
  if (!filePath.startsWith(path.join(workspace, 'Exports'))) {
    return res.status(403).json({ error: 'Forbidden' });
  }

  if (!fs.existsSync(filePath)) {
    return res.status(404).json({ error: 'File not found' });
  }

  res.download(filePath);
});

// Setup User Configuration
app.post('/api/setup-user', (req, res) => {
  const { userName } = req.body;

  if (!userName || userName.trim().length === 0) {
    return res.status(400).json({ error: 'Username cannot be empty' });
  }

  try {
    // Read current config
    let configContent = fs.readFileSync(CONFIG_PATH, 'utf8');

    // Replace or add USER_NAME
    if (configContent.includes('USER_NAME=')) {
      configContent = configContent.replace(/^USER_NAME=.*$/m, `USER_NAME=${userName.trim()}`);
    } else {
      configContent = `USER_NAME=${userName.trim()}\n${configContent}`;
    }

    // Write back config
    fs.writeFileSync(CONFIG_PATH, configContent, 'utf8');

    // Update in-memory pythonEnvVars
    pythonEnvVars['USER_NAME'] = userName.trim();

    console.log(`✓ User configured: ${userName.trim()}`);
    res.json({ success: true, userName: userName.trim() });
  } catch (err) {
    console.error('Error saving user config:', err);
    res.status(500).json({ error: err.message });
  }
});

// Setup Screen (user-override-aware)
app.get('/setup', (req, res) => { sendPublic(res, 'setup.html'); });

// Splash Screen
app.get('/splash', (req, res) => { sendPublic(res, 'splash.html'); });

// Main Dashboard
app.get('/', (req, res) => { sendPublic(res, 'dashboard.html'); });

// ═══ UPDATE MANAGEMENT ═══

// Global update state
let updateState = {
  current: 'idle',           // idle, checking, available, downloading, downloaded, installing, error
  latestVersion: null,
  available: false,
  downloadSize: 0,
  downloadUrl: null,         // Store for use during install
  releaseNotes: '',
  releaseDate: null,
  progress: {percent: 0, speed: 0, eta: null},
  lastChecked: null,
  error: null
};

// GitHub repository info (from package.json)
const GITHUB_OWNER = 'PostProSuite';
const GITHUB_REPO = 'Lampenwelt.de';

// Get app version from package.json (works in both dev and packaged)
function getAppVersion() {
  try {
    // In packaged app, package.json is inside app.asar (readable)
    const pkg = require('./package.json');
    return pkg.version;
  } catch (err) {
    return '0.0.0';
  }
}

// Compare semantic versions: returns -1, 0, or 1
function compareVersions(a, b) {
  const parse = v => String(v).replace(/^v/, '').split('.').map(n => parseInt(n, 10) || 0);
  const [a1, a2, a3] = parse(a);
  const [b1, b2, b3] = parse(b);
  if (a1 !== b1) return a1 < b1 ? -1 : 1;
  if (a2 !== b2) return a2 < b2 ? -1 : 1;
  if (a3 !== b3) return a3 < b3 ? -1 : 1;
  return 0;
}

// Query GitHub API for latest release
async function fetchLatestGitHubRelease() {
  const https = require('https');
  return new Promise((resolve, reject) => {
    const options = {
      hostname: 'api.github.com',
      path: `/repos/${GITHUB_OWNER}/${GITHUB_REPO}/releases/latest`,
      method: 'GET',
      headers: {
        'User-Agent': 'PostPro-Suite-Updater',
        'Accept': 'application/vnd.github.v3+json'
      },
      timeout: 10000
    };

    const req = https.request(options, (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => {
        try {
          if (res.statusCode === 200) {
            resolve(JSON.parse(data));
          } else {
            reject(new Error(`GitHub API returned ${res.statusCode}`));
          }
        } catch (err) {
          reject(err);
        }
      });
    });
    req.on('error', reject);
    req.on('timeout', () => { req.destroy(); reject(new Error('Request timeout')); });
    req.end();
  });
}

// GET /api/update-status
app.get('/api/update-status', (req, res) => {
  res.json({
    state: updateState.current,
    currentVersion: getAppVersion(),
    latestVersion: updateState.latestVersion,
    available: updateState.available,
    downloadSize: updateState.downloadSize,
    releaseNotes: updateState.releaseNotes,
    releaseDate: updateState.releaseDate,
    progress: updateState.progress,
    lastChecked: updateState.lastChecked,
    error: updateState.error
  });
});

// POST /api/check-updates
// Actually queries GitHub API and compares versions
app.post('/api/check-updates', async (req, res) => {
  updateState.current = 'checking';
  updateState.error = null;

  try {
    const release = await fetchLatestGitHubRelease();
    updateState.lastChecked = new Date().toISOString();

    const latestVersion = (release.tag_name || '').replace(/^v/, '');
    const currentVersion = getAppVersion();
    const comparison = compareVersions(currentVersion, latestVersion);

    // Find DMG asset to get download size and URL
    const dmgAsset = (release.assets || []).find(a => a.name.includes('.dmg') && a.name.includes('arm64'));
    const downloadSize = dmgAsset ? dmgAsset.size : 0;
    const downloadUrl = dmgAsset ? dmgAsset.browser_download_url : null;

    updateState.latestVersion = latestVersion;
    updateState.downloadSize = downloadSize;
    updateState.downloadUrl = downloadUrl;
    updateState.releaseNotes = release.body || '';
    updateState.releaseDate = release.published_at || null;

    if (comparison < 0) {
      // Current version is older than latest
      updateState.available = true;
      updateState.current = 'available';
    } else {
      updateState.available = false;
      updateState.current = 'idle';
    }

    res.json({
      available: updateState.available,
      currentVersion: currentVersion,
      latestVersion: latestVersion,
      downloadSize: downloadSize,
      downloadSizeMB: (downloadSize / 1024 / 1024).toFixed(1),
      releaseNotes: updateState.releaseNotes,
      releaseDate: updateState.releaseDate,
      lastChecked: updateState.lastChecked
    });
  } catch (err) {
    updateState.current = 'error';
    updateState.error = err.message;
    res.status(500).json({error: err.message, lastChecked: updateState.lastChecked});
  }
});

// POST /api/download-update
// Uses CustomUpdater to download, mount, and install the app
app.post('/api/download-update', async (req, res) => {
  if (!updateState.downloadUrl) {
    return res.status(400).json({error: 'No download URL available. Check for updates first.'});
  }

  updateState.current = 'downloading';
  updateState.progress = {percent: 0, speed: 0, eta: null};
  updateState.error = null;

  // SSE Headers for streaming progress
  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-cache');
  res.setHeader('Connection', 'keep-alive');

  try {
    const currentVersion = getAppVersion();
    const updater = new CustomUpdater(currentVersion);

    const timestamp = new Date().toLocaleTimeString('de-DE');
    res.write(`data: ${JSON.stringify({
      type: 'log',
      text: `[${timestamp}] 📥 Starte Update-Installation für v${updateState.latestVersion}...`,
      color: 'blue'
    })}\n\n`);

    // Call install with progress callback
    await updater.install(updateState.downloadUrl, (progress) => {
      updateState.progress = {
        percent: progress.percent,
        downloaded: progress.downloaded,
        total: progress.total,
        speed: progress.speed
      };

      res.write(`data: ${JSON.stringify({
        type: 'progress',
        percent: progress.percent,
        downloaded: progress.downloaded,
        total: progress.total,
        speed: progress.speed + ' MB/s'
      })}\n\n`);
    });

    // Installation complete - app is replaced
    updateState.current = 'downloaded';
    const doneTimestamp = new Date().toLocaleTimeString('de-DE');
    res.write(`data: ${JSON.stringify({
      type: 'log',
      text: `[${doneTimestamp}] ✓ App erfolgreich aktualisiert. Klicke auf "Jetzt Installieren" zum Neustart.`,
      color: 'green'
    })}\n\n`);
    res.write(`data: ${JSON.stringify({
      type: 'done',
      status: 'success'
    })}\n\n`);
    res.end();
  } catch (err) {
    updateState.current = 'error';
    updateState.error = err.message;
    const errorTimestamp = new Date().toLocaleTimeString('de-DE');
    res.write(`data: ${JSON.stringify({
      type: 'log',
      text: `[${errorTimestamp}] ❌ Update-Fehler: ${err.message}`,
      color: 'red'
    })}\n\n`);
    res.write(`data: ${JSON.stringify({
      type: 'done',
      status: 'error',
      error: err.message
    })}\n\n`);
    res.end();
  }
});

// POST /api/install-update
// Signals main.js to restart the app with the new version
app.post('/api/install-update', (req, res) => {
  try {
    updateState.current = 'installing';
    // Write a flag file that main.js will detect to trigger restart
    const flagPath = path.join(os.tmpdir(), 'postpro-restart-flag');
    fs.writeFileSync(flagPath, JSON.stringify({
      timestamp: new Date().toISOString(),
      action: 'restart'
    }));
    res.json({success: true, message: 'Neustart wird eingeleitet...'});
  } catch (err) {
    updateState.current = 'error';
    res.status(500).json({error: err.message});
  }
});

// POST /api/cancel-update
app.post('/api/cancel-update', (req, res) => {
  updateState.current = 'idle';
  updateState.progress = {percent: 0, speed: 0, eta: null};

  res.json({cancelled: true});
});

// ════════════════════════════════════════════════════════════════
// DELTA-UPDATES (Skripte/UI ohne DMG-Download)
// ════════════════════════════════════════════════════════════════

// GET /api/check-script-updates
// Prüft welche Files sich gegenüber GitHub geändert haben (ohne Download)
app.get('/api/check-script-updates', async (req, res) => {
  try {
    const result = await checkScriptUpdatesAvailable(__dirname);

    if (result.error) {
      return res.status(503).json({
        available: false,
        error: result.error
      });
    }

    res.json({
      available: result.changed.length > 0,
      changedCount: result.changed.length,
      totalFiles: result.total,
      changedFiles: result.changed,
      manifestVersion: result.manifestVersion,
    });
  } catch (err) {
    console.error('check-script-updates Fehler:', err);
    res.status(500).json({error: err.message});
  }
});

// POST /api/apply-script-updates
// Führt Delta-Update sofort aus (ohne App-Neustart)
app.post('/api/apply-script-updates', async (req, res) => {
  try {
    console.log('🔄 Manuelles Delta-Update via Update-Button gestartet...');
    const result = await checkAndUpdateScripts(__dirname);

    res.json({
      success: result.errors.length === 0,
      updatedCount: result.updated.length,
      updatedFiles: result.updated,
      skipped: result.skipped,
      errors: result.errors,
      // UI-Files brauchen Reload, Python-Files nicht
      uiUpdated: result.updated.some(f => f.startsWith('public/')),
      pythonUpdated: result.updated.some(f => f.endsWith('.py') || f.endsWith('.mlmodel')),
    });
  } catch (err) {
    console.error('apply-script-updates Fehler:', err);
    res.status(500).json({error: err.message});
  }
});

// POST /api/reload-window
// Triggert Browser-Reload nach UI-Update (über main.js)
app.post('/api/reload-window', (req, res) => {
  try {
    const flagPath = path.join(os.tmpdir(), 'postpro-reload-flag');
    fs.writeFileSync(flagPath, JSON.stringify({timestamp: new Date().toISOString()}));
    res.json({success: true});
  } catch (err) {
    res.status(500).json({error: err.message});
  }
});

// Fallback für alle anderen Routes (SPA)
app.use((req, res) => { sendPublic(res, 'dashboard.html'); });

module.exports = app;
