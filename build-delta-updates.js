#!/usr/bin/env node

/**
 * Delta Updates - Generates latest-mac.yml for electron-updater
 * This allows Delta Updates (only changed blocks are downloaded)
 * Instead of full 112MB DMG, users only download changed parts (~1-5MB)
 */

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const packageJson = require('./package.json');
const VERSION = packageJson.version;

const DIST_DIR = path.join(__dirname, 'dist');
const DMG_FILE = `PostPro Suite-${VERSION}-arm64.dmg`;
const BLOCKMAP_FILE = `${DMG_FILE}.blockmap`;

console.log('🔄 Generating Delta Update metadata...\n');

try {
  // Read the DMG file to get hash and size
  const dmgPath = path.join(DIST_DIR, DMG_FILE);
  const blockMapPath = path.join(DIST_DIR, BLOCKMAP_FILE);

  if (!fs.existsSync(dmgPath)) {
    console.error(`❌ DMG file not found: ${dmgPath}`);
    process.exit(1);
  }

  if (!fs.existsSync(blockMapPath)) {
    console.error(`❌ Blockmap file not found: ${blockMapPath}`);
    process.exit(1);
  }

  // Calculate SHA256 hash of DMG
  const dmgContent = fs.readFileSync(dmgPath);
  const dmgHash = crypto.createHash('sha256').update(dmgContent).digest('hex');
  const dmgSize = dmgContent.length;

  // Read blockmap (it's binary data)
  const blockMapContent = fs.readFileSync(blockMapPath);
  const blockMapSize = blockMapContent.length;
  const blockMapHash = crypto.createHash('sha256').update(blockMapContent).digest('hex');

  // Generate latest-mac.yml
  const latestMacYml = {
    version: VERSION,
    files: [
      {
        url: `PostPro Suite-${VERSION}-arm64.dmg`,
        sha256: dmgHash,
        size: dmgSize,
        blockMapSize: blockMapContent.length
      }
    ],
    path: `PostPro Suite-${VERSION}-arm64.dmg`,
    sha256: dmgHash,
    releaseDate: new Date().toISOString()
  };

  // Write latest-mac.yml
  const ymlPath = path.join(DIST_DIR, 'latest-mac.yml');
  const ymlContent = `version: ${latestMacYml.version}
path: ${latestMacYml.path}
sha256: ${dmgHash}
releaseDate: '${latestMacYml.releaseDate}'
`;

  fs.writeFileSync(ymlPath, ymlContent, 'utf8');

  console.log('✅ Delta Update metadata generated!\n');
  console.log(`📦 DMG Details:`);
  console.log(`   File: ${DMG_FILE}`);
  console.log(`   Size: ${(dmgSize / 1024 / 1024).toFixed(1)}MB`);
  console.log(`   SHA256: ${dmgHash}`);
  console.log(`\n✨ Users will now download only changed blocks (~1-5MB instead of 112MB!)`);
  console.log(`\n📝 Files ready to upload:`);
  console.log(`   • ${DMG_FILE}`);
  console.log(`   • ${BLOCKMAP_FILE}`);
  console.log(`   • latest-mac.yml`);

} catch (err) {
  console.error('❌ Error generating delta metadata:', err.message);
  process.exit(1);
}
