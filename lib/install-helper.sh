#!/bin/bash
###############################################################################
# PostPro Suite Update Installer Helper
#
# Replaces the running app with a new version by:
#   1. Waiting for the running app process to exit
#   2. Replacing the .app bundle (atomic mv + cp)
#   3. Removing macOS quarantine flag
#   4. Unmounting the source DMG
#   5. Restarting the app
#
# Args:
#   $1 = PID of running app (will wait for it to exit)
#   $2 = TARGET .app path (e.g. /Applications/PostPro Suite.app)
#   $3 = SOURCE .app path (e.g. /Volumes/PostPro Suite 1.2.36-arm64/PostPro Suite.app)
#   $4 = LOG_FILE (optional, default: /tmp/postpro-updater.log)
###############################################################################

PARENT_PID="$1"
TARGET_APP="$2"
SOURCE_APP="$3"
LOG_FILE="${4:-/tmp/postpro-updater.log}"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

log "════════════════════════════════════════"
log "PostPro Updater Helper started"
log "PID:    $PARENT_PID"
log "TARGET: $TARGET_APP"
log "SOURCE: $SOURCE_APP"

# Validate args
if [ -z "$PARENT_PID" ] || [ -z "$TARGET_APP" ] || [ -z "$SOURCE_APP" ]; then
    log "ERROR: Missing args"
    exit 1
fi

# 1) Wait for parent app to exit (max 30s)
log "Waiting for app PID $PARENT_PID to exit..."
for i in {1..60}; do
    if ! kill -0 "$PARENT_PID" 2>/dev/null; then
        log "App exited (after ${i}/60 checks)"
        break
    fi
    sleep 0.5
done

# Force-kill if still running
if kill -0 "$PARENT_PID" 2>/dev/null; then
    log "App still running after 30s, sending SIGTERM"
    kill -TERM "$PARENT_PID" 2>/dev/null || true
    sleep 2
    if kill -0 "$PARENT_PID" 2>/dev/null; then
        log "App still running, sending SIGKILL"
        kill -KILL "$PARENT_PID" 2>/dev/null || true
        sleep 1
    fi
fi

# Extra safety wait for file locks to release
sleep 2

# 2) Verify source app exists
if [ ! -d "$SOURCE_APP" ]; then
    log "ERROR: Source app not found: $SOURCE_APP"
    osascript -e "display alert \"PostPro Update fehlgeschlagen\" message \"Quell-App nicht gefunden:\n$SOURCE_APP\nLog: $LOG_FILE\" as critical" || true
    exit 1
fi

# 3) Backup current app (safety net)
TARGET_BACKUP="${TARGET_APP}.bak"
if [ -d "$TARGET_APP" ]; then
    log "Creating backup: $TARGET_BACKUP"
    rm -rf "$TARGET_BACKUP" 2>/dev/null
    if ! mv "$TARGET_APP" "$TARGET_BACKUP"; then
        log "ERROR: Could not move target to backup. Permission issue?"
        osascript -e "display alert \"PostPro Update fehlgeschlagen\" message \"App kann nicht ersetzt werden. Bitte App manuell aus DMG installieren.\nLog: $LOG_FILE\" as critical" || true
        # Open DMG in Finder so user can drag manually
        open "$(dirname "$SOURCE_APP")"
        exit 1
    fi
fi

# 4) Copy new app
log "Copying $SOURCE_APP -> $TARGET_APP"
if cp -R "$SOURCE_APP" "$TARGET_APP"; then
    log "Copy OK"

    # 5) Remove macOS quarantine flag (so it can run without "unidentified developer" warning)
    xattr -cr "$TARGET_APP" 2>/dev/null || true
    log "Quarantine flag removed"

    # 6) Remove backup (success!)
    rm -rf "$TARGET_BACKUP" 2>/dev/null
    log "Backup removed"

    # 7) Unmount source DMG (parent of SOURCE_APP if /Volumes/...)
    SOURCE_PARENT=$(dirname "$SOURCE_APP")
    if [[ "$SOURCE_PARENT" == /Volumes/* ]]; then
        log "Unmounting $SOURCE_PARENT"
        hdiutil detach "$SOURCE_PARENT" -force 2>/dev/null || true
    fi

    # 8) Restart app
    log "Starting new app: $TARGET_APP"
    open "$TARGET_APP"
    log "✓ Update completed successfully"
    exit 0
else
    log "ERROR: cp failed, restoring backup"
    rm -rf "$TARGET_APP" 2>/dev/null
    if [ -d "$TARGET_BACKUP" ]; then
        mv "$TARGET_BACKUP" "$TARGET_APP"
        log "Backup restored"
    fi
    osascript -e "display alert \"PostPro Update fehlgeschlagen\" message \"Beim Kopieren der neuen Version trat ein Fehler auf.\nLog: $LOG_FILE\" as critical" || true
    exit 1
fi
