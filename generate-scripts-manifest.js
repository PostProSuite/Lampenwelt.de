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

const SCRIPTS_DIR = path.join(__dirname, 'src', 'scripts');
const MANIFEST_PATH = path.join(__dirname, 'scripts-manifest.json');

// File patterns to INCLUDE in manifest
const INCLUDE_EXTENSIONS = ['.py', '.mlmodel'];

// Files/dirs to EXCLUDE
const EXCLUDE_PATTERNS = [
  '__pycache__',
  '.DS_Store',
  '.pyc',
  'JSON',         // Generated JSON files - not source
  'config',       // User-specific config
];

function shouldInclude(filePath, relativePath) {
  // Exclude patterns
  for (const pattern of EXCLUDE_PATTERNS) {
    if (relativePath.includes(pattern)) return false;
  }
  // Include extensions
  const ext = path.extname(filePath);
  return INCLUDE_EXTENSIONS.includes(ext);
}

function walkDir(dir, baseDir = dir, fileList = []) {
  const files = fs.readdirSync(dir);
  for (const file of files) {
    const fullPath = path.join(dir, file);
    const relativePath = path.relative(baseDir, fullPath);
    const stat = fs.statSync(fullPath);

    if (stat.isDirectory()) {
      // Skip excluded dirs
      if (EXCLUDE_PATTERNS.some(p => file === p)) continue;
      walkDir(fullPath, baseDir, fileList);
    } else if (shouldInclude(fullPath, relativePath)) {
      fileList.push({ fullPath, relativePath });
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

  if (!fs.existsSync(SCRIPTS_DIR)) {
    console.error(`❌ Scripts directory not found: ${SCRIPTS_DIR}`);
    process.exit(1);
  }

  const files = walkDir(SCRIPTS_DIR);
  const manifest = {
    version: pkg.version,
    generated: new Date().toISOString(),
    description: 'Manifest for delta-update system - used by script-updater.js',
    files: {}
  };

  let totalSize = 0;
  for (const { fullPath, relativePath } of files) {
    const stat = fs.statSync(fullPath);
    manifest.files[relativePath] = {
      sha256: fileHash(fullPath),
      size: stat.size,
      mtime: stat.mtime.toISOString(),
    };
    totalSize += stat.size;
  }

  fs.writeFileSync(MANIFEST_PATH, JSON.stringify(manifest, null, 2));

  console.log(`✓ Manifest generated: ${MANIFEST_PATH}`);
  console.log(`   Files: ${files.length}`);
  console.log(`   Total size: ${(totalSize / 1024).toFixed(1)} KB`);
  console.log(`   Version: ${manifest.version}`);
}

generateManifest();
