/**
 * Script Delta-Updater
 *
 * Lädt einzelne geänderte Python-Skripte aus dem GitHub-Repo,
 * ohne dass die ganze DMG neu installiert werden muss.
 *
 * Ablauf:
 *   1. scripts-manifest.json vom Repo laden
 *   2. Lokale Skripte gegen Manifest-Hashes vergleichen
 *   3. Nur geänderte Dateien herunterladen
 *   4. In User-Override-Verzeichnis speichern (app bevorzugt diese)
 */

const fs = require('fs');
const path = require('path');
const https = require('https');
const crypto = require('crypto');
const os = require('os');

const GITHUB_RAW_BASE = 'https://raw.githubusercontent.com/PostProSuite/Lampenwelt.de/main';
const MANIFEST_URL = `${GITHUB_RAW_BASE}/scripts-manifest.json`;

/**
 * User-writable override directory.
 * App bevorzugt Skripte hier gegenüber den eingebauten (app.asar.unpacked).
 */
function getUserScriptsDir() {
  const base = path.join(os.homedir(), 'Library', 'Application Support', 'PostPro Suite', 'scripts');
  if (!fs.existsSync(base)) {
    fs.mkdirSync(base, { recursive: true });
  }
  return base;
}

/**
 * Pfad zur bundled Skripte (read-only, in app.asar.unpacked)
 */
function getBundledScriptsDir(appDir) {
  if (appDir.includes('app.asar')) {
    return path.join(appDir.replace('app.asar', 'app.asar.unpacked'), 'src', 'scripts');
  }
  return path.join(appDir, 'src', 'scripts');
}

/**
 * HTTPS GET als Promise - mit Timeout
 */
function httpsGet(url, timeout = 30000) {
  return new Promise((resolve, reject) => {
    const req = https.get(url, { timeout }, (res) => {
      if (res.statusCode === 302 || res.statusCode === 301) {
        // Handle redirect
        return httpsGet(res.headers.location, timeout).then(resolve).catch(reject);
      }
      if (res.statusCode !== 200) {
        return reject(new Error(`HTTP ${res.statusCode} for ${url}`));
      }
      let data = Buffer.alloc(0);
      res.on('data', chunk => { data = Buffer.concat([data, chunk]); });
      res.on('end', () => resolve(data));
      res.on('error', reject);
    });
    req.on('error', reject);
    req.on('timeout', () => {
      req.destroy();
      reject(new Error('Request timeout'));
    });
  });
}

/**
 * SHA256-Hash einer Datei berechnen
 */
function fileHash(filePath) {
  if (!fs.existsSync(filePath)) return null;
  const content = fs.readFileSync(filePath);
  return crypto.createHash('sha256').update(content).digest('hex');
}

/**
 * Remote manifest laden
 */
async function fetchRemoteManifest() {
  try {
    const buf = await httpsGet(MANIFEST_URL, 15000);
    return JSON.parse(buf.toString('utf8'));
  } catch (err) {
    console.warn('⚠️ Konnte Remote-Manifest nicht laden:', err.message);
    return null;
  }
}

/**
 * Eine einzelne Datei vom GitHub-Repo laden und speichern
 */
async function downloadScript(relativePath, targetDir, expectedHash = null) {
  const url = `${GITHUB_RAW_BASE}/src/scripts/${relativePath}`;
  const targetPath = path.join(targetDir, relativePath);

  // Ensure directory exists
  const targetDirname = path.dirname(targetPath);
  if (!fs.existsSync(targetDirname)) {
    fs.mkdirSync(targetDirname, { recursive: true });
  }

  const content = await httpsGet(url, 30000);

  // Verify hash if expected
  if (expectedHash) {
    const hash = crypto.createHash('sha256').update(content).digest('hex');
    if (hash !== expectedHash) {
      throw new Error(`Hash-Mismatch für ${relativePath}: expected ${expectedHash}, got ${hash}`);
    }
  }

  // Atomic write: tmp file → rename
  const tmpPath = targetPath + '.tmp';
  fs.writeFileSync(tmpPath, content);
  fs.renameSync(tmpPath, targetPath);

  return targetPath;
}

/**
 * Hauptfunktion: Prüft auf geänderte Skripte und lädt sie runter.
 * Gibt Anzahl der aktualisierten Dateien zurück.
 *
 * @param {string} appDir - app directory (path.join(__dirname))
 * @returns {Promise<{updated: string[], errors: string[]}>}
 */
async function checkAndUpdateScripts(appDir) {
  const result = { updated: [], errors: [], skipped: 0 };

  try {
    const remoteManifest = await fetchRemoteManifest();
    if (!remoteManifest || !remoteManifest.files) {
      console.log('ℹ Kein Remote-Manifest verfügbar - überspringe Script-Updates');
      return result;
    }

    const userDir = getUserScriptsDir();
    const bundledDir = getBundledScriptsDir(appDir);

    for (const [relativePath, meta] of Object.entries(remoteManifest.files)) {
      try {
        const userFile = path.join(userDir, relativePath);
        const bundledFile = path.join(bundledDir, relativePath);

        // Which file is the "current" one?
        // Priority: user-override > bundled
        const currentFile = fs.existsSync(userFile) ? userFile : bundledFile;
        const currentHash = fileHash(currentFile);

        if (currentHash === meta.sha256) {
          // Up-to-date - no download needed
          result.skipped++;
          continue;
        }

        // Needs update - download to user dir (overrides bundled)
        console.log(`📥 Update für ${relativePath}`);
        await downloadScript(relativePath, userDir, meta.sha256);
        result.updated.push(relativePath);
      } catch (err) {
        console.warn(`⚠️ Fehler bei ${relativePath}:`, err.message);
        result.errors.push(`${relativePath}: ${err.message}`);
      }
    }

    if (result.updated.length > 0) {
      console.log(`✓ ${result.updated.length} Skripte aktualisiert`);
    }
    if (result.skipped > 0) {
      console.log(`ℹ ${result.skipped} Skripte bereits aktuell`);
    }

    return result;
  } catch (err) {
    console.error('❌ Delta-Update fehlgeschlagen:', err.message);
    result.errors.push(err.message);
    return result;
  }
}

/**
 * Helper: gibt den EFFEKTIVEN Pfad zu einem Skript zurück.
 * Bevorzugt user-override (frisch geladen) über bundled.
 *
 * Von server.js verwendet um Python-Skripte zu starten.
 */
function resolveScriptPath(appDir, relativePath) {
  const userPath = path.join(getUserScriptsDir(), relativePath);
  if (fs.existsSync(userPath)) {
    return userPath;
  }
  const bundledPath = path.join(getBundledScriptsDir(appDir), relativePath);
  return bundledPath;
}

module.exports = {
  checkAndUpdateScripts,
  resolveScriptPath,
  getUserScriptsDir,
  getBundledScriptsDir,
};
