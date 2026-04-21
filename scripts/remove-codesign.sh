#!/bin/bash
# Remove code signatures from built app to prevent electron-updater validation errors

APP_PATH="dist/mac-arm64/PostPro Suite.app"

if [ ! -d "$APP_PATH" ]; then
  echo "❌ App not found at $APP_PATH"
  exit 1
fi

echo "🔓 Removing code signatures from app..."
codesign --remove-signature "$APP_PATH" 2>&1

# Verify signature was removed
if ! codesign -v "$APP_PATH" 2>&1 | grep -q "not signed at all"; then
  echo "⚠️ Warning: Signature may still be present"
  exit 1
fi

echo "✓ Code signatures removed successfully"
exit 0
