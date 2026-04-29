const fs = require('fs');
const path = require('path');
const os = require('os');
const { execSync } = require('child_process');

class Installer {
  constructor() {
    // In packaged app, __dirname points to app.asar, but unpacked files are in app.asar.unpacked
    this.appDir = __dirname.includes('app.asar')
      ? __dirname.replace('app.asar', 'app.asar.unpacked')
      : __dirname;
    this.home = os.homedir();
    this.workspace = path.join(this.home, 'Desktop', 'Temp', 'C-In_Progress', 'PostPro Suite');
  }

  // Setup Workspace Ordnerstruktur
  setupWorkspace() {
    const dirs = [
      this.workspace,
      path.join(this.workspace, '01-Input RAW files'),
      // 02-Webcheck mit allen Export-Unterordnern (für Lightroom-Export)
      path.join(this.workspace, '02-Webcheck'),
      path.join(this.workspace, '02-Webcheck', '01-Mainimage'),
      path.join(this.workspace, '02-Webcheck', '02-Mood'),
      path.join(this.workspace, '02-Webcheck', '03-Pos4-X'),
      path.join(this.workspace, '03-Upload'),
      path.join(this.workspace, 'Exports'),
      path.join(this.workspace, 'logs'),
    ];

    dirs.forEach(dir => {
      if (!fs.existsSync(dir)) {
        fs.mkdirSync(dir, { recursive: true });
        console.log('✓ Erstellt:', dir);
      }
    });
    console.log('✓ Workspace ready\n');
  }

  // Config-Datei in scripts/config kopieren
  copyConfig() {
    const configDir = path.join(this.appDir, 'src', 'scripts', 'config');
    const scriptConfig = path.join(configDir, 'config.env');

    // Stelle sicher dass der config Ordner existiert
    if (!fs.existsSync(configDir)) {
      fs.mkdirSync(configDir, { recursive: true });
      console.log('✓ Config-Ordner erstellt:', configDir);
    }

    if (!fs.existsSync(scriptConfig)) {
      // Wenn config.env nicht existiert, DefaultConfig erstellen
      const defaultConfig = `# PostPro Suite Config
USER_NAME=
CLIPLISTER_CLIENT_ID=55e39d5e-a052-3d62-9f99-417992d92173
CLIPLISTER_CLIENT_SECRET=d8605e06-31a3-3ca9-a389-665cb89e5c55
SFTP_HOST=clup01.cliplister.com
SFTP_PORT=4545
SFTP_USERNAME=lw01
SFTP_PASSWORD=2ZAoUFfNGgVvcBOfxZ
JIRA_SERVER=https://lampenwelt.atlassian.net
JIRA_EMAIL=sven.bonafede@lampenwelt.de
JIRA_API_TOKEN=ATATT3xFfGF0Eb2UEpY3ffkne7h6hCy2NEsgZ4ZuDhPaQliN5r0b7AVGPb8vWQE1hO3zRRTfRmmKl_zUBdDaiILgA02LX9UtCAwLIPRq9i_-t3__p_hjUvWDQpsE5UMldy6xB4yQsNqgcVGdRlgdhu4e93bZe7t52qGROTZx4zt4L7ydQL13-7E=5F11A759
POSTPRO_WORKSPACE=${this.workspace}
LOG_LEVEL=INFO
LOG_FILE=/dev/null
API_REQUEST_TIMEOUT=120
LIGHTROOM_STARTUP_DELAY=8
API_REQUEST_DELAY=0.2
ASYNC_TASK_CONCURRENCY=8
`;
      fs.writeFileSync(scriptConfig, defaultConfig, 'utf8');
      console.log('✓ Config.env erstellt:', scriptConfig);
    }
  }

  // Python Dependencies checken & installieren
  checkPythonDeps() {
    console.log('🔍 Checke Python Dependencies...');
    try {
      execSync('python3 -c "import requests, paramiko, PIL, openpyxl, aiohttp, dotenv, jira"', { stdio: 'pipe' });
      console.log('✓ Alle Python-Pakete vorhanden\n');
      return true;
    } catch (err) {
      console.log('⚠ Fehlende Python-Pakete. Installation wird empfohlen:\n');
      console.log('   pip3 install -r requirements.txt\n');
      return false;
    }
  }

  // Alles zusammen
  run() {
    console.log('\n╔════════════════════════════════════╗');
    console.log('║   PostPro Suite - First Run Setup   ║');
    console.log('╚════════════════════════════════════╝\n');

    this.setupWorkspace();
    this.copyConfig();
    this.checkPythonDeps();

    console.log('✅ Setup abgeschlossen!\n');
  }
}

if (require.main === module) {
  new Installer().run();
}

module.exports = Installer;
