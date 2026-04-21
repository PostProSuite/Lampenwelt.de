const { app, BrowserWindow, Menu, dialog } = require('electron');
const { autoUpdater } = require('electron-updater');
const path = require('path');
const http = require('http');
const fs = require('fs');
const expressApp = require('./server.js');
const Installer = require('./installer.js');
let httpServer;

let mainWindow;

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

app.on('ready', async () => {
  // First run setup
  try {
    new Installer().run();
  } catch (err) {
    console.error('Setup error:', err);
  }

  await startServer();
  createWindow();

  // Auto-Updates checken
  autoUpdater.checkForUpdatesAndNotify();
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
autoUpdater.on('update-available', () => {
  console.log('✓ Update verfügbar');
});

autoUpdater.on('update-downloaded', () => {
  console.log('✓ Update heruntergeladen');
  dialog.showMessageBox(mainWindow, {
    type: 'info',
    title: 'Update bereit',
    message: 'PostPro Suite wurde aktualisiert.',
    detail: 'Die neue Version ist bereit. Jetzt neu starten?',
    buttons: ['Jetzt neu starten', 'Später'],
    defaultId: 0,
    cancelId: 1,
  }).then(({ response }) => {
    if (response === 0) autoUpdater.quitAndInstall();
  });
});
