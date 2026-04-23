/**
 * Generate scripts-manifest.json
 *
 * Erstellt eine Liste aller Python-Skripte mit SHA256-Hashes
 * für das Delta-Update-System.
 *
 * Usage: node generate-scripts-manifest.js
 */

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

// Delta-update: Python scripts + UI (HTML/CSS/JS)
// Updates zu diesen Dateien brauchen KEIN neues DMG
const UPDATE_SOURCES = [
  { dir: 'src/scripts', exts: ['.py', '.mlmodel'] },
  { dir: 'public',      exts: ['.html', '.css', '.js', '.svg'] },
];

const MANIFEST_PATH = path.join(__dirname, 'scripts-manifest.json');

// Files/dirs to EXCLUDE
const EXCLUDE_PATTERNS = [
  '__pycache__',
  '.DS_Store',
  '.pyc',
  'JSON',         // Generated JSON files - not source
  'config',       // User-specific config
  'assets',       // Large binaries/icons - keep bundled
];

function shouldInclude(filePath, relativePath, allowedExts) {
  // Exclude patterns
  for (const pattern of EXCLUDE_PATTERNS) {
    if (relativePath.includes(pattern)) return false;
  }
  const ext = path.extname(filePath);
  return allowedExts.includes(ext);
}

function walkDir(dir, baseDir, allowedExts, pathPrefix, fileList = []) {
  if (!fs.existsSync(dir)) return fileList;
  const files = fs.readdirSync(dir);
  for (const file of files) {
    const fullPath = path.join(dir, file);
    const relativeToBase = path.relative(baseDir, fullPath);
    const relativeToPrefix = path.join(pathPrefix, relativeToBase);
    const stat = fs.statSync(fullPath);

    if (stat.isDirectory()) {
      if (EXCLUDE_PATTERNS.some(p => file === p)) continue;
      walkDir(fullPath, baseDir, allowedExts, pathPrefix, fileList);
    } else if (shouldInclude(fullPath, relativeToBase, allowedExts)) {
      fileList.push({ fullPath, relativePath: relativeToPrefix });
    }
  }
  return fileList;
}

function fileHash(filePath) {
  const content = fs.readFileSync(filePath);
  return crypto.createHash('sha256').update(content).digest('hex');
}

function generateManifest() {
  console.log('📝 Generating scripts-manifest.json...');

  const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, 'package.json'), 'utf8'));

  const allFiles = [];
  for (const source of UPDATE_SOURCES) {
    const dir = path.join(__dirname, source.dir);
    // pathPrefix = source.dir so 'scripts/foo.py' becomes 'src/scripts/foo.py' etc.
    const files = walkDir(dir, dir, source.exts, source.dir);
    allFiles.push(...files);
  }

  if (allFiles.length === 0) {
    console.error('❌ No files found in any UPDATE_SOURCES');
    process.exit(1);
  }

  const manifest = {
    version: pkg.version,
    generated: new Date().toISOString(),
    description: 'Manifest for delta-update system - used by script-updater.js',
    files: {}
  };

  let totalSize = 0;
  for (const { fullPath, relativePath } of allFiles) {
    const stat = fs.statSync(fullPath);
    // Normalize path separators for cross-platform
    const normalizedPath = relativePath.split(path.sep).join('/');
    manifest.files[normalizedPath] = {
      sha256: fileHash(fullPath),
      size: stat.size,
      mtime: stat.mtime.toISOString(),
    };
    totalSize += stat.size;
  }

  fs.writeFileSync(MANIFEST_PATH, JSON.stringify(manifest, null, 2));

  console.log(`✓ Manifest generated: ${MANIFEST_PATH}`);
  console.log(`   Files: ${allFiles.length}`);
  console.log(`   Total size: ${(totalSize / 1024).toFixed(1)} KB`);
  console.log(`   Version: ${manifest.version}`);
}

generateManifest();
