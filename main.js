const { app, BrowserWindow, Menu } = require('electron');
const { autoUpdater } = require('electron-updater');
const path = require('path');
const isDev = require('electron-is-dev');
let server;

let mainWindow;

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

  // Starte Server, dann lade die App
  if (isDev) {
    mainWindow.loadURL('http://localhost:3000');
    mainWindow.webContents.openDevTools();
  } else {
    mainWindow.loadURL('http://localhost:3000');
  }

  mainWindow.on('closed', () => {
    mainWindow = null;
    if (server) server.close();
    app.quit();
  });
}

// Server starten
function startServer() {
  return new Promise((resolve) => {
    server = require('./server.js');
    server.listen(3000, () => {
      console.log('✓ Server läuft auf http://localhost:3000');
      resolve();
    });
  });
}

app.on('ready', async () => {
  await startServer();
  createWindow();

  // Auto-Updates checken
  if (!isDev) {
    autoUpdater.checkForUpdatesAndNotify();
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
autoUpdater.on('update-available', () => {
  console.log('✓ Update verfügbar');
});

autoUpdater.on('update-downloaded', () => {
  console.log('✓ Update heruntergeladen - App wird neu gestartet');
  autoUpdater.quitAndInstall();
});
