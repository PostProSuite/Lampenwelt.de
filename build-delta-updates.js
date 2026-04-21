#!/usr/bin/env node

/**
 * Delta Updates - Generates latest-mac.yml for electron-updater
 * This allows Delta Updates (only changed blocks are downloaded)
 * Uses sha512 and proper files[] array format expected by electron-updater 6.x
 */

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const packageJson = require('./package.json');
const VERSION = packageJson.version;

const DIST_DIR = path.join(__dirname, 'dist');
const DMG_FILE = `PostPro-Suite-${VERSION}-arm64.dmg`;
const ZIP_FILE = `PostPro-Suite-${VERSION}-arm64-mac.zip`;
const DMG_BLOCKMAP = `${DMG_FILE}.blockmap`;
const ZIP_BLOCKMAP = `${ZIP_FILE}.blockmap`;

console.log('🔄 Generating Delta Update metadata...\n');

function sha512Base64(filePath) {
  const content = fs.readFileSync(filePath);
  return crypto.createHash('sha512').update(content).digest('base64');
}

function fileSize(filePath) {
  return fs.statSync(filePath).size;
}

try {
  const dmgPath = path.join(DIST_DIR, DMG_FILE);
  const zipPath = path.join(DIST_DIR, ZIP_FILE);
  const dmgBlockmapPath = path.join(DIST_DIR, DMG_BLOCKMAP);
  const zipBlockmapPath = path.join(DIST_DIR, ZIP_BLOCKMAP);

  if (!fs.existsSync(dmgPath)) {
    console.error(`❌ DMG file not found: ${dmgPath}`);
    process.exit(1);
  }
  if (!fs.existsSync(dmgBlockmapPath)) {
    console.error(`❌ DMG blockmap not found: ${dmgBlockmapPath}`);
    process.exit(1);
  }

  // Compute hashes and sizes
  const dmgSha512 = sha512Base64(dmgPath);
  const dmgSize = fileSize(dmgPath);
  const dmgBlockMapSize = fileSize(dmgBlockmapPath);

  // Build files array (zip first for electron-updater preference, then dmg)
  const files = [];

  if (fs.existsSync(zipPath)) {
    const zipSha512 = sha512Base64(zipPath);
    const zipSize = fileSize(zipPath);
    const zipBlockMapSize = fs.existsSync(zipBlockmapPath) ? fileSize(zipBlockmapPath) : 0;
    files.push({
      url: ZIP_FILE,
      sha512: zipSha512,
      size: zipSize,
      blockMapSize: zipBlockMapSize,
    });
  }

  files.push({
    url: DMG_FILE,
    sha512: dmgSha512,
    size: dmgSize,
    blockMapSize: dmgBlockMapSize,
  });

  // Generate YAML (proper electron-updater format)
  const yamlLines = [
    `version: ${VERSION}`,
    `files:`,
  ];

  for (const f of files) {
    yamlLines.push(`  - url: ${f.url}`);
    yamlLines.push(`    sha512: ${f.sha512}`);
    yamlLines.push(`    size: ${f.size}`);
    yamlLines.push(`    blockMapSize: ${f.blockMapSize}`);
  }

  // Top-level path + sha512 (electron-updater uses the primary file here - prefer zip for updates)
  const primary = files[0];
  yamlLines.push(`path: ${primary.url}`);
  yamlLines.push(`sha512: ${primary.sha512}`);
  yamlLines.push(`releaseDate: '${new Date().toISOString()}'`);

  const ymlPath = path.join(DIST_DIR, 'latest-mac.yml');
  fs.writeFileSync(ymlPath, yamlLines.join('\n') + '\n', 'utf8');

  console.log('✅ Delta Update metadata generated!\n');
  console.log(`📦 Files included in latest-mac.yml:`);
  for (const f of files) {
    console.log(`   • ${f.url} (${(f.size / 1024 / 1024).toFixed(1)} MB)`);
  }
  console.log(`\n✨ Users now download only changed blocks (~1-5MB instead of 112MB)`);
  console.log(`\n📝 Files ready to upload to GitHub Release:`);
  console.log(`   • ${DMG_FILE}`);
  console.log(`   • ${DMG_BLOCKMAP}`);
  if (fs.existsSync(zipPath)) {
    console.log(`   • ${ZIP_FILE}`);
    console.log(`   • ${ZIP_BLOCKMAP}`);
  }
  console.log(`   • latest-mac.yml`);

} catch (err) {
  console.error('❌ Error generating delta metadata:', err.message);
  console.error(err.stack);
  process.exit(1);
}
