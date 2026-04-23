const { app, BrowserWindow, Menu, dialog, ipcMain } = require('electron');
const { autoUpdater } = require('electron-updater');
const path = require('path');
const http = require('http');
const fs = require('fs');
const { execSync } = require('child_process');
const expressApp = require('./server.js');
const Installer = require('./installer.js');
const { checkAndUpdateScripts } = require('./script-updater.js');

let httpServer;

let mainWindow;
let updateAvailableInfo = null;

// CRITICAL: Disable code signature verification in electron-updater
// (App is unsigned - no certificate, prevents "Code signature did not pass validation" errors)
// Override the verifyUpdateCodeSignature method to skip validation
const originalVerify = autoUpdater.verifyUpdateCodeSignature;
if (originalVerify) {
  autoUpdater.verifyUpdateCodeSignature = () => {
    console.log('⏭️  Code signature verification skipped (unsigned development build)');
    return Promise.resolve(true);
  };
}

// Check if app is on a read-only volume (e.g. DMG or Downloads folder)
function isAppOnReadOnlyLocation() {
  if (!app.isPackaged) return false;  // Ignore in dev mode
  const appPath = app.getAppPath();
  // App should be in /Applications (/System/Applications is system apps)
  const isInApplications = appPath.includes('/Applications/') && !appPath.includes('/Volumes/');
  return !isInApplications;
}

// Prompt user to move app to Applications folder on startup
function promptMoveToApplications() {
  if (!app.isPackaged) return;
  if (process.platform !== 'darwin') return;
  if (!isAppOnReadOnlyLocation()) return;

  const choice = dialog.showMessageBoxSync({
    type: 'warning',
    buttons: ['Jetzt verschieben und neu starten', 'Ignorieren'],
    defaultId: 0,
    cancelId: 1,
    title: 'App muss in "Programme" installiert werden',
    message: 'PostPro Suite läuft gerade von einem schreibgeschützten Ort.',
    detail: 'Automatische Updates funktionieren nur, wenn die App im Ordner "Programme" liegt.\n\nAktueller Pfad:\n' + app.getAppPath() + '\n\nSoll die App jetzt automatisch nach /Programme verschoben werden? Die App wird dann neu gestartet.'
  });

  if (choice === 0) {
    try {
      app.moveToApplicationsFolder({
        conflictHandler: (conflictType) => {
          if (conflictType === 'exists') {
            const overwrite = dialog.showMessageBoxSync({
              type: 'question',
              buttons: ['Überschreiben', 'Abbrechen'],
              defaultId: 0,
              cancelId: 1,
              message: 'Eine andere Version von PostPro Suite existiert bereits in /Programme.',
              detail: 'Soll diese ersetzt werden?'
            });
            return overwrite === 0;
          }
          return true;
        }
      });
    } catch (err) {
      dialog.showErrorBox('Verschieben fehlgeschlagen',
        'Die App konnte nicht automatisch verschoben werden.\n\n' +
        'Bitte manuell: App aus dem aktuellen Ordner in /Programme ziehen.\n\n' +
        'Fehler: ' + err.message);
    }
  }
}

// IPC handler: renderer can query installation location
ipcMain.handle('get-install-info', () => {
  return {
    isPackaged: app.isPackaged,
    appPath: app.getAppPath(),
    isReadOnly: isAppOnReadOnlyLocation(),
    platform: process.platform
  };
});

// IPC: trigger move to Applications folder
ipcMain.handle('move-to-applications', async () => {
  try {
    if (process.platform !== 'darwin') {
      return {success: false, error: 'Nur auf macOS verfügbar'};
    }
    app.moveToApplicationsFolder();
    return {success: true};
  } catch (err) {
    return {success: false, error: err.message};
  }
});

// Check if user is configured
function isUserConfigured() {
  // In packaged app, files are in app.asar.unpacked
  const configPath = __dirname.includes('app.asar')
    ? path.join(__dirname.replace('app.asar', 'app.asar.unpacked'), 'src', 'scripts', 'config', 'config.env')
    : path.join(__dirname, 'src', 'scripts', 'config', 'config.env');

  if (!fs.existsSync(configPath)) return false;

  const content = fs.readFileSync(configPath, 'utf8');
  const userNameMatch = content.match(/^USER_NAME=(.*)$/m);
  const userName = userNameMatch ? userNameMatch[1].trim() : '';
  return userName.length > 0;
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      nodeIntegration: false,
      contextIsolation: true,
    },
  });

  // Starte Server, dann lade die App (Setup wenn User nicht konfiguriert, sonst Splash)
  const startPage = isUserConfigured() ? '/splash' : '/setup';
  mainWindow.loadURL(`http://localhost:3000${startPage}`);

  mainWindow.on('closed', () => {
    mainWindow = null;
    if (httpServer) {
      httpServer.close(() => {
        console.log('Server beendet');
        app.quit();
      });
    } else {
      app.quit();
    }
  });
}

// Server starten
function startServer() {
  return new Promise((resolve, reject) => {
    try {
      httpServer = http.createServer(expressApp);

      // Ermögliche schnelle Wiederverwendung des Ports
      httpServer.on('error', (err) => {
        if (err.code === 'EADDRINUSE') {
          console.error('Port 3000 ist bereits belegt. Versuche in 2 Sekunden erneut...');
          setTimeout(() => {
            httpServer.close();
            startServer().then(resolve).catch(reject);
          }, 2000);
        } else {
          reject(err);
        }
      });

      httpServer.listen(3000, () => {
        console.log('✓ Server läuft auf http://localhost:3000');
        resolve();
      });
    } catch (err) {
      console.error('Fehler beim Starten des Servers:', err);
      reject(err);
    }
  });
}

// Setup auto-update checking: on startup + every 24 hours
function setupAutoUpdateCheck() {
  // Check on app ready
  autoUpdater.checkForUpdatesAndNotify();

  // Check every 24 hours
  setInterval(() => {
    console.log('🔄 Auto-check für Updates (24h)...');
    autoUpdater.checkForUpdatesAndNotify();
  }, 24 * 60 * 60 * 1000);
}

// Check if python3 is available on the system
function checkPython3Available() {
  try {
    const version = execSync('python3 --version', { encoding: 'utf8', stdio: 'pipe' });
    console.log(`🐍 Python gefunden: ${version.trim()}`);
    return true;
  } catch (err) {
    return false;
  }
}

app.on('ready', async () => {
  // Check if app is in /Applications (needed for auto-updates on macOS)
  promptMoveToApplications();

  // Resolve setup script path (works in packaged and dev)
  const setupScriptPath = __dirname.includes('app.asar')
    ? path.join(__dirname.replace('app.asar', 'app.asar.unpacked'), 'src', 'scripts', 'setup_python_env.py')
    : path.join(__dirname, 'src', 'scripts', 'setup_python_env.py');

  // CRITICAL: Check if python3 is installed at all
  if (!checkPython3Available()) {
    const choice = dialog.showMessageBoxSync({
      type: 'error',
      buttons: ['Python installieren (Browser)', 'App beenden'],
      defaultId: 0,
      cancelId: 1,
      title: 'Python 3 nicht gefunden',
      message: 'Python 3 ist nicht auf diesem System installiert.',
      detail: 'PostPro Suite benötigt Python 3 zur Ausführung der Workflows.\n\nInstallationsoptionen:\n1. Download von python.org\n2. Homebrew: brew install python3\n3. macOS-System-Version (meist bereits installiert)'
    });
    if (choice === 0) {
      require('electron').shell.openExternal('https://www.python.org/downloads/');
    }
    app.quit();
    return;
  }

  // Setup Python environment - check and install missing dependencies
  const requiredPackages = ['requests', 'paramiko', 'Pillow', 'openpyxl', 'aiohttp', 'python-dotenv', 'jira', 'cryptography', 'urllib3<2.0', 'numpy'];
  let pythonSetupSuccess = false;

  try {
    console.log('🐍 Prüfe Python-Dependencies...');
    execSync(`python3 "${setupScriptPath}"`, { stdio: 'inherit', timeout: 600000 });
    console.log('✓ Python-Environment bereit');
    pythonSetupSuccess = true;
  } catch (err) {
    console.error('⚠️ Python-Setup fehlgeschlagen:', err.message);

    // Show interactive dialog: user can choose to install packages now
    const choice = dialog.showMessageBoxSync({
      type: 'warning',
      buttons: ['Jetzt installieren', 'Später', 'Abbrechen'],
      defaultId: 0,
      cancelId: 2,
      title: 'Python-Packages fehlen',
      message: 'Einige Python-Packages sind nicht installiert.',
      detail: 'PostPro Suite benötigt folgende Packages:\n' + requiredPackages.join(', ') + '\n\nMöchten Sie diese jetzt installieren?\n\n(Dies öffnet das Terminal für die Installation)'
    });

    if (choice === 0) {
      // User clicked "Jetzt installieren"
      console.log('🔧 Installiere Python-Packages mit pip...');
      try {
        const pipCmd = `python3 -m pip install --user ${requiredPackages.join(' ')}`;
        execSync(pipCmd, { stdio: 'inherit', shell: '/bin/bash' });
        console.log('✓ Python-Packages erfolgreich installiert');
        pythonSetupSuccess = true;
      } catch (pipErr) {
        console.error('❌ Pip-Installation fehlgeschlagen:', pipErr.message);
        dialog.showErrorBox(
          'Installation fehlgeschlagen',
          'Die automatische Installation hat nicht funktioniert.\n\n' +
          'Bitte führen Sie folgende Kommando manuell im Terminal aus:\n\n' +
          `pip3 install --user ${requiredPackages.join(' ')}\n\n` +
          'Nach der Installation bitte die App neu starten.'
        );
      }
    } else if (choice === 1) {
      // User clicked "Später"
      console.warn('⚠️ Python-Setup aufgeschoben');
      dialog.showWarningBox(
        'Python-Setup später',
        'Sie können die Installation später durchführen.\n\n' +
        'Workflows können nicht ausgeführt werden, bis alle Packages installiert sind.\n\n' +
        'Terminal-Kommando:\n' +
        `pip3 install --user ${requiredPackages.join(' ')}`
      );
    } else {
      // User clicked "Abbrechen"
      console.error('❌ App startup cancelled - Python packages missing');
      app.quit();
      return;
    }
  }

  // First run setup
  try {
    new Installer().run();
  } catch (err) {
    console.error('Setup error:', err);
  }

  // Delta-Update: Check for individual updated Python scripts from GitHub
  // This lets us fix bugs by pushing only the changed script files
  // without requiring users to download the full DMG again.
  try {
    console.log('🔄 Prüfe auf Skript-Updates (Delta-Update)...');
    const updateResult = await checkAndUpdateScripts(__dirname);
    if (updateResult.updated.length > 0) {
      console.log(`✓ ${updateResult.updated.length} Skripte aktualisiert:`, updateResult.updated);
    }
    if (updateResult.errors.length > 0) {
      console.warn(`⚠ ${updateResult.errors.length} Skripte mit Fehlern`);
    }
  } catch (err) {
    console.warn('⚠ Skript-Delta-Update nicht möglich (offline?):', err.message);
    // Don't block startup if we can't update scripts
  }

  await startServer();
  createWindow();

  // Setup auto-update checking (only if app is in Applications)
  if (!isAppOnReadOnlyLocation()) {
    setupAutoUpdateCheck();
  } else {
    console.warn('⚠️ Auto-Updates deaktiviert: App nicht in /Programme');
  }
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

app.on('activate', () => {
  if (mainWindow === null) {
    createWindow();
  }
});

// Helper: Remove quarantine attribute from downloaded app (macOS blocks unsigned apps from internet)
function removeQuarantineFlag() {
  try {
    const cacheDir = path.join(app.getPath('cache'), 'com.postprosuite.app.ShipIt');
    if (fs.existsSync(cacheDir)) {
      // Find and remove quarantine flag from any downloaded app
      execSync(`find "${cacheDir}" -name "PostPro Suite.app" -exec xattr -d com.apple.quarantine {} \\; 2>/dev/null || true`);
      console.log('✓ Quarantine-Flag entfernt');
    }
  } catch (err) {
    console.warn('⚠️ Konnte Quarantine-Flag nicht entfernen:', err.message);
  }
}

// Helper: Patch electron-updater to skip signature validation for unsigned apps
function patchElectronUpdaterValidation() {
  try {
    // Get the internal stageHandler and patch it
    const { AppUpdater } = require('electron-updater');
    if (AppUpdater && AppUpdater.prototype) {
      const originalStageHandler = AppUpdater.prototype.stageHandler;
      if (originalStageHandler) {
        AppUpdater.prototype.stageHandler = function(event, version) {
          console.log('⏭️  Skipping code signature validation for unsigned development build');
          // Skip the validation - just continue
          return Promise.resolve();
        };
      }
    }
  } catch (err) {
    console.warn('⚠️ Could not patch stageHandler:', err.message);
  }
}

// Call the patch on startup
patchElectronUpdaterValidation();

// Auto-Update Events
autoUpdater.on('checking-for-update', () => {
  console.log('🔍 Prüfe auf Updates...');
  if (mainWindow) {
    mainWindow.webContents.send('update-status', {state: 'checking'});
  }
});

autoUpdater.on('update-available', (info) => {
  console.log('✓ Update verfügbar:', info.version);
  updateAvailableInfo = info;
  if (mainWindow) {
    mainWindow.webContents.send('update-available', {
      version: info.version,
      releaseDate: info.releaseDate,
      files: info.files
    });
  }
});

autoUpdater.on('update-not-available', () => {
  console.log('✓ App ist aktuell');
  if (mainWindow) {
    mainWindow.webContents.send('update-status', {state: 'idle'});
  }
});

autoUpdater.on('download-progress', (progress) => {
  console.log(`📥 Download: ${progress.percent.toFixed(1)}%`);
  if (mainWindow) {
    mainWindow.webContents.send('download-progress', {
      percent: progress.percent,
      bytesPerSecond: progress.bytesPerSecond,
      transferred: progress.transferred,
      total: progress.total
    });
  }
});

autoUpdater.on('update-downloaded', () => {
  console.log('✓ Update heruntergeladen und bereit zur Installation');
  // Remove quarantine flag so macOS doesn't block the unsigned app
  removeQuarantineFlag();
  if (mainWindow) {
    mainWindow.webContents.send('update-status', {state: 'downloaded'});
  }
});

autoUpdater.on('error', (err) => {
  // Skip code signature validation errors for development builds (unsigned app)
  if (err.message && err.message.includes('Code signature')) {
    console.warn('⏭️  Code signature validation error IGNORED (unsigned development build)');
    // Continue anyway - force retry or manual install
    // This prevents blocking on signature validation
    return;
  }
  console.error('❌ Update-Fehler:', err.message);
  if (mainWindow) {
    mainWindow.webContents.send('update-error', {error: err.message});
  }
});

// IPC Handlers for update operations
ipcMain.handle('check-for-updates', async () => {
  try {
    const result = await autoUpdater.checkForUpdates();
    return {
      success: true,
      updateInfo: result ? result.updateInfo : null
    };
  } catch (err) {
    console.error('checkForUpdates error:', err);
    if (mainWindow) {
      mainWindow.webContents.send('update-error', {error: err.message});
    }
    return {success: false, error: err.message};
  }
});

ipcMain.handle('download-update', async () => {
  try {
    // Ensure electron-updater has update info by checking first
    const checkResult = await autoUpdater.checkForUpdates();
    if (!checkResult || !checkResult.updateInfo) {
      throw new Error('Kein Update verfügbar');
    }
    // Now start the actual download
    await autoUpdater.downloadUpdate();
    return {success: true};
  } catch (err) {
    console.error('downloadUpdate error:', err);
    if (mainWindow) {
      mainWindow.webContents.send('update-error', {error: err.message});
    }
    throw err;
  }
});

ipcMain.handle('install-update', () => {
  try {
    // Remove quarantine flag before installing
    removeQuarantineFlag();
    autoUpdater.quitAndInstall(false, true);
    return {success: true};
  } catch (err) {
    console.error('installUpdate error:', err);
    if (mainWindow) {
      mainWindow.webContents.send('update-error', {error: err.message});
    }
    return {success: false, error: err.message};
  }
});

// Also remove quarantine flag before macOS tries to execute the updated app
app.on('before-quit-for-update', () => {
  console.log('🔓 Entferne Quarantine-Flag vor Update-Installation...');
  removeQuarantineFlag();
});
