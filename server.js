const express = require('express');
const cors = require('cors');
const path = require('path');
const http = require('http');
const session = require('express-session');
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const app = express();

// In the packaged .app, Python scripts are unpacked outside the .asar archive
const SCRIPTS_DIR = __dirname.includes('app.asar')
  ? path.join(__dirname.replace('app.asar', 'app.asar.unpacked'), 'src', 'scripts')
  : path.join(__dirname, 'src', 'scripts');

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
const WORKSPACE_FOLDERS = [
  '01-Input RAW files',
  '02-Webcheck',
  '03-Upload',
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

// Statische Dateien
app.use(express.static(path.join(__dirname, 'public')));

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

  // Build command
  const scriptPath = path.join(SCRIPTS_DIR, workflow.script);
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

  // Start Python process with config environment variables + input value
  const spawnEnv = { ...pythonEnvVars };
  if (input_value) spawnEnv['POSTPRO_INPUT'] = input_value;
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

// Setup Screen
app.get('/setup', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'setup.html'));
});

// Splash Screen
app.get('/splash', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'splash.html'));
});

// Main Dashboard
app.get('/', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'dashboard.html'));
});

// ═══ UPDATE MANAGEMENT ═══

// Global update state
let updateState = {
  current: 'idle',           // idle, checking, available, downloading, downloaded, installing, error
  latestVersion: null,
  available: false,
  downloadSize: 0,
  progress: {percent: 0, speed: 0, eta: null},
  lastChecked: null
};

// Get app version from package.json
function getAppVersion() {
  const pkg = require('./package.json');
  return pkg.version;
}

// GET /api/update-status
app.get('/api/update-status', (req, res) => {
  res.json({
    state: updateState.current,
    currentVersion: getAppVersion(),
    latestVersion: updateState.latestVersion,
    available: updateState.available,
    downloadSize: updateState.downloadSize,
    progress: updateState.progress,
    lastChecked: updateState.lastChecked
  });
});

// POST /api/check-updates
app.post('/api/check-updates', async (req, res) => {
  try {
    updateState.current = 'checking';
    updateState.lastChecked = new Date().toISOString();

    // This is called from main.js via IPC, but we'll return status
    // The actual checking happens in main.js and IPC sends results
    res.json({
      available: updateState.available,
      version: updateState.latestVersion,
      downloadSize: updateState.downloadSize
    });
  } catch (err) {
    updateState.current = 'error';
    res.status(500).json({error: err.message});
  }
});

// POST /api/download-update
app.post('/api/download-update', (req, res) => {
  updateState.current = 'downloading';

  // SSE response for download progress
  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-cache');
  res.setHeader('Connection', 'keep-alive');

  // Send initial message
  res.write(`data: ${JSON.stringify({type: 'log', text: 'Starting download...', color: 'blue'})}\n\n`);

  // Setup IPC listener for progress updates from main.js
  // The main process will call ipcMain to send progress events
  // which we capture via a global event emitter (would need to implement)

  // For now, just indicate it's started - the download happens in main.js
  setTimeout(() => {
    res.write(`data: ${JSON.stringify({type: 'done', status: 'download_started'})}\n\n`);
    res.end();
  }, 500);
});

// POST /api/install-update
app.post('/api/install-update', (req, res) => {
  updateState.current = 'installing';

  try {
    // Installation is triggered from main.js
    // App will restart, so we don't need to do much here
    res.json({success: true, message: 'Update installation initiated'});
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

// Fallback für alle anderen Routes (SPA)
app.use((req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'dashboard.html'));
});

module.exports = app;
