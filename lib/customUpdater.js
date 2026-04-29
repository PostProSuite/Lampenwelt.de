/**
 * Custom Update Manager for PostPro Suite
 * Handles updates without relying on electron-updater code signature validation
 * Downloads DMG, mounts it, then a Helper-Script replaces the app AFTER quit.
 *
 * Workflow:
 *  1. Download DMG from GitHub
 *  2. Mount DMG → get source app path
 *  3. Spawn install-helper.sh in detached background process
 *     - Helper waits for THIS app process to exit
 *     - Helper replaces app, removes quarantine, unmounts DMG, relaunches
 *  4. App quits → Helper takes over → New app starts
 */

const https = require('https');
const path = require('path');
const fs = require('fs');
const os = require('os');
const { execSync, spawn } = require('child_process');

class CustomUpdater {
  constructor(appVersion) {
    this.appVersion = appVersion;
    this.githubOwner = 'PostProSuite';
    this.githubRepo = 'Lampenwelt.de';
    this.currentDownload = null;
    this.appPath = this.getAppPath();
  }

  /**
   * Get current app path from shared file or fallback to /Applications
   */
  getAppPath() {
    try {
      const infoPath = path.join(os.tmpdir(), 'postpro-app-info.json');
      if (fs.existsSync(infoPath)) {
        const info = JSON.parse(fs.readFileSync(infoPath, 'utf8'));
        console.log(`📱 Using app path from file: ${info.appPath}`);
        return info.appPath;
      }
    } catch (err) {
      console.warn(`⚠️ Could not read app path file: ${err.message}`);
    }

    // Fallback for packaged app in /Applications
    console.log('📱 Using fallback app path: /Applications');
    return '/Applications';
  }

  /**
   * Check latest release on GitHub
   */
  async checkLatestRelease() {
    return new Promise((resolve, reject) => {
      const url = `https://api.github.com/repos/${this.githubOwner}/${this.githubRepo}/releases/latest`;

      https.get(url, {
        headers: { 'User-Agent': 'PostProSuite-Updater' }
      }, (res) => {
        let data = '';
        res.on('data', chunk => data += chunk);
        res.on('end', () => {
          try {
            const json = JSON.parse(data);

            if (json.message === 'Not Found') {
              reject(new Error('No releases found on GitHub'));
              return;
            }

            const version = json.tag_name.replace('v', '');
            const dmgAsset = json.assets.find(a => a.name.includes('.dmg') && a.name.includes('arm64'));

            if (!dmgAsset) {
              reject(new Error('No DMG found for arm64 architecture'));
              return;
            }

            resolve({
              version,
              downloadUrl: dmgAsset.browser_download_url,
              size: dmgAsset.size,
              releaseDate: json.published_at
            });
          } catch (err) {
            reject(err);
          }
        });
      }).on('error', reject);
    });
  }

  /**
   * Download DMG from GitHub with progress callback
   */
  async downloadDMG(downloadUrl, onProgress) {
    const filename = 'PostPro-Suite-update.dmg';
    const tmpDir = path.join(os.tmpdir(), 'postpro-update');
    const filepath = path.join(tmpDir, filename);

    // Create temp directory
    if (!fs.existsSync(tmpDir)) {
      fs.mkdirSync(tmpDir, { recursive: true });
    }

    return new Promise((resolve, reject) => {
      const file = fs.createWriteStream(filepath);
      let downloadedBytes = 0;
      let totalBytes = 0;
      const startTime = Date.now();

      const request = https.get(downloadUrl, {
        headers: { 'User-Agent': 'PostProSuite-Updater' }
      }, (res) => {
        // Handle redirects
        if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
          file.destroy();
          console.log(`Following redirect to ${res.headers.location}`);
          return this.downloadDMG(res.headers.location, onProgress).then(resolve).catch(reject);
        }

        totalBytes = parseInt(res.headers['content-length'], 10) || 0;
        console.log(`Download started. Content-Length: ${totalBytes} bytes`);

        res.on('data', (chunk) => {
          downloadedBytes += chunk.length;
          const elapsedSeconds = (Date.now() - startTime) / 1000;
          const speedMbps = (downloadedBytes / 1024 / 1024 / elapsedSeconds).toFixed(1);
          const percent = totalBytes > 0 ? Math.round((downloadedBytes / totalBytes) * 100) : 0;
          const mbDownloaded = (downloadedBytes / 1024 / 1024).toFixed(1);
          const mbTotal = (totalBytes / 1024 / 1024).toFixed(1);

          if (onProgress) {
            onProgress({
              percent,
              downloaded: mbDownloaded,
              total: mbTotal,
              speed: speedMbps
            });
          }
        });

        res.pipe(file);
      });

      request.on('error', (err) => {
        file.destroy();
        fs.unlink(filepath, () => {});
        reject(err);
      });

      file.on('finish', () => {
        file.close();
        const finalSize = fs.statSync(filepath).size;
        console.log(`Download finished. File size: ${(finalSize / 1024 / 1024).toFixed(1)}MB`);

        if (finalSize === 0) {
          fs.unlink(filepath, () => {});
          reject(new Error('Downloaded file is empty'));
          return;
        }

        resolve(filepath);
      });

      file.on('error', (err) => {
        fs.unlink(filepath, () => {});
        reject(err);
      });
    });
  }

  /**
   * Mount DMG file
   */
  mountDMG(dmgPath) {
    return new Promise((resolve, reject) => {
      try {
        // Add slight delay to ensure file is written
        setTimeout(() => {
          const fs = require('fs');

          // Verify DMG exists and is readable
          if (!fs.existsSync(dmgPath)) {
            reject(new Error(`DMG file not found: ${dmgPath}`));
            return;
          }

          const stats = fs.statSync(dmgPath);
          if (stats.size === 0) {
            reject(new Error(`DMG file is empty: ${dmgPath}`));
            return;
          }

          console.log(`Mounting DMG: ${dmgPath} (${(stats.size / 1024 / 1024).toFixed(1)}MB)`);

          const output = execSync(`hdiutil attach "${dmgPath}" -nobrowse 2>&1`, { encoding: 'utf8' });
          const mountPoint = output.split('\n').find(line => line.includes('/Volumes/'))?.split('\t')[2]?.trim();

          if (!mountPoint) {
            console.log('hdiutil output:', output);
            reject(new Error('Could not find mount point in hdiutil output'));
            return;
          }

          console.log(`✓ Mounted at: ${mountPoint}`);

          // Wait a moment for mount to stabilize
          setTimeout(() => resolve(mountPoint), 1000);
        }, 500);
      } catch (err) {
        console.error(`hdiutil error: ${err.message}`);
        reject(new Error(`Mount failed: ${err.message}`));
      }
    });
  }

  /**
   * Unmount DMG
   */
  unmountDMG(mountPoint) {
    return new Promise((resolve, reject) => {
      try {
        execSync(`hdiutil detach "${mountPoint}" 2>&1`, { encoding: 'utf8' });
        setTimeout(() => resolve(), 500);
      } catch (err) {
        // Ignore errors if already unmounted
        resolve();
      }
    });
  }

  /**
   * Resolve the actual .app path from the Electron appPath.
   * appPath aus app-info.json kann sein:
   *   /Applications/PostPro Suite.app/Contents/Resources/app.asar  (packaged)
   *   /Users/.../Lampenwelt.de                                      (dev mode)
   *   /Applications                                                  (fallback)
   */
  resolveTargetAppPath() {
    const p = this.appPath;

    // Packaged app: path ends with app.asar
    if (p.includes('.asar')) {
      // Go up: app.asar → Resources → Contents → PostPro Suite.app
      const dotApp = path.dirname(path.dirname(path.dirname(p)));
      console.log(`📦 Packaged app detected, .app path: ${dotApp}`);
      return dotApp;
    }

    // Path already points to .app (some scenarios)
    if (p.endsWith('.app')) {
      return p;
    }

    // Dev mode: appPath is the source directory → put .app next to it
    console.log(`🛠  Dev mode detected, placing .app next to source: ${p}`);
    return path.join(p, 'PostPro Suite.app');
  }

  /**
   * Find the helper script (install-helper.sh)
   */
  getHelperPath() {
    // Try multiple locations - bundled in app or repo dev
    const candidates = [
      path.join(__dirname, 'install-helper.sh'),
      path.join(process.resourcesPath || '', 'app.asar.unpacked', 'lib', 'install-helper.sh'),
      path.join(process.resourcesPath || '', 'lib', 'install-helper.sh'),
    ];
    for (const c of candidates) {
      if (c && fs.existsSync(c)) {
        return c;
      }
    }
    return null;
  }

  /**
   * Spawn helper script that will replace the app AFTER our process exits.
   * The helper waits, then mv/cp, then unmounts DMG, then relaunches app.
   *
   * @param {string} mountPoint - DMG mount point (e.g. /Volumes/PostPro Suite x.x.x)
   * @returns {{helperPath:string, sourceApp:string, targetApp:string, parentPid:number, logFile:string}}
   */
  spawnHelperForReplace(mountPoint) {
    const sourceApp = path.join(mountPoint, 'PostPro Suite.app');
    const targetApp = this.resolveTargetAppPath();

    console.log(`🔄 Preparing helper-based replace`);
    console.log(`   Target: ${targetApp}`);
    console.log(`   Source: ${sourceApp}`);

    if (!fs.existsSync(sourceApp)) {
      throw new Error(`Source app not found: ${sourceApp}`);
    }

    // Try to copy helper to a writable location AND make executable
    const helperPath = this.getHelperPath();
    if (!helperPath) {
      throw new Error('Helper script install-helper.sh not found in any expected location');
    }

    // Copy helper to /tmp so it remains accessible after DMG unmount
    const tmpHelper = path.join(os.tmpdir(), 'postpro-install-helper.sh');
    fs.copyFileSync(helperPath, tmpHelper);
    fs.chmodSync(tmpHelper, 0o755);

    const logFile = path.join(os.tmpdir(), 'postpro-updater.log');
    const parentPid = process.pid;

    console.log(`   Helper:    ${tmpHelper}`);
    console.log(`   Log:       ${logFile}`);
    console.log(`   ParentPID: ${parentPid}`);

    // Spawn DETACHED so it survives app quit
    const child = spawn('/bin/bash', [
      tmpHelper,
      String(parentPid),
      targetApp,
      sourceApp,
      logFile,
    ], {
      detached: true,
      stdio: 'ignore',
    });
    child.unref();

    console.log(`✓ Helper spawned with PID ${child.pid}`);

    return {
      helperPath: tmpHelper,
      sourceApp,
      targetApp,
      parentPid,
      logFile,
      helperPid: child.pid,
    };
  }

  /**
   * Clean up downloaded files
   */
  cleanup(dmgPath) {
    try {
      if (fs.existsSync(dmgPath)) {
        fs.unlinkSync(dmgPath);
      }
      const tmpDir = path.dirname(dmgPath);
      if (fs.existsSync(tmpDir) && fs.readdirSync(tmpDir).length === 0) {
        fs.rmdirSync(tmpDir);
      }
    } catch (err) {
      console.warn('Could not clean up temp files:', err.message);
    }
  }

  /**
   * Main install flow.
   * Phase 1: Download + Mount (synchronous, can be done while app runs)
   * Phase 2: Helper takes over AFTER this function returns (spawnHelperForReplace)
   *
   * After this returns successfully, the caller MUST:
   *   1. Quit the Electron app within ~5 seconds
   *   2. The helper will replace the app + restart it
   */
  async install(downloadUrl, onProgress) {
    let dmgPath = null;
    let mountPoint = null;

    try {
      console.log('📥 Starting update installation...');

      // Download
      console.log('⬇️ Downloading DMG...');
      dmgPath = await this.downloadDMG(downloadUrl, onProgress);
      console.log('✓ DMG downloaded:', dmgPath);

      // Mount
      console.log('📦 Mounting DMG...');
      mountPoint = await this.mountDMG(dmgPath);
      console.log('✓ DMG mounted at:', mountPoint);

      // Spawn helper that will run AFTER our process exits
      console.log('🚀 Spawning install helper (will replace after app quits)...');
      const helperInfo = this.spawnHelperForReplace(mountPoint);
      console.log('✓ Helper ready - app must quit now');

      // NOTE: We do NOT unmount here - helper will unmount after copying.
      // We do NOT cleanup DMG - it's still mounted; will be cleaned up next session.
      // Caller is responsible for quitting the app.

      return {
        success: true,
        helperPid: helperInfo.helperPid,
        targetApp: helperInfo.targetApp,
        logFile: helperInfo.logFile,
        message: 'Update bereit - App muss jetzt beendet werden für Installation',
      };
    } catch (err) {
      // Cleanup on error
      if (mountPoint) {
        try {
          await this.unmountDMG(mountPoint);
        } catch (e) {
          // Ignore
        }
      }
      this.cleanup(dmgPath);

      console.error('❌ Installation failed:', err.message);
      throw err;
    }
  }
}

module.exports = CustomUpdater;
