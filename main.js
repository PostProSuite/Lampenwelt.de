const { app, BrowserWindow, Menu, dialog, ipcMain } = require('electron');
const { autoUpdater } = require('electron-updater');
const path = require('path');
const http = require('http');
const fs = require('fs');
const expressApp = require('./server.js');
const Installer = require('./installer.js');
let httpServer;

let mainWindow;
let updateAvailableInfo = null;

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

app.on('ready', async () => {
  // Check if app is in /Applications (needed for auto-updates on macOS)
  promptMoveToApplications();

  // First run setup
  try {
    new Installer().run();
  } catch (err) {
    console.error('Setup error:', err);
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
  if (mainWindow) {
    mainWindow.webContents.send('update-status', {state: 'downloaded'});
  }
});

autoUpdater.on('error', (err) => {
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
