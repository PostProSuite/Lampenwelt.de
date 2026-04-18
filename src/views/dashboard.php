<?php
/**
 * PostPro Suite — Dashboard
 * Workflow-Cards, Log-Panel, Settings, Admin
 */
require_once __DIR__ . '/../includes/workflows.php';
$categories = get_categories();
$workflows = get_workflows();
$isAdmin = ($user['role'] === 'admin');
?>
<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PostPro Suite</title>
<link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/postpro/assets/css/style.css">
</head>
<body>

<!-- ═══ APP ═══ -->
<div class="app" id="app">

  <!-- SIDEBAR -->
  <aside class="sidebar">
    <div class="sidebar-logo">
      <div class="sidebar-logo-text">PostPro Suite</div>
      <div class="sidebar-logo-sub">LUQOM GROUP</div>
    </div>
    <div class="sidebar-divider"></div>
    <nav class="nav">
      <a class="nav-item active" data-view="workflows" href="#">
        <span class="nav-icon">&#9654;</span> Workflows
      </a>
      <a class="nav-item" data-view="history" href="#">
        <span class="nav-icon">📊</span> Exports
      </a>
      <a class="nav-item" data-view="settings" href="#">
        <span class="nav-icon">&#9881;</span> Einstellungen
      </a>
    </nav>
    <div class="sidebar-spacer"></div>
    <nav class="nav" style="padding-bottom:16px">
      <a class="nav-item support-link" href="https://support.demoup-cliplister.com/" target="_blank">
        <span class="nav-icon">&#10067;</span> Hilfe
      </a>
    </nav>
  </aside>

  <!-- MAIN -->
  <div class="main">

    <!-- HEADER -->
    <div class="header">
      <img class="header-logo-icon" src="/postpro/assets/logo/LUQOM-Icon.svg" alt="LUQOM">
      <span class="header-title">PostPro Suite</span>
      <span class="header-version">v<?= APP_VERSION ?></span>
    </div>

    <!-- ═══ VIEW: WORKFLOWS ═══ -->
    <div class="view active" id="view-workflows">
      <div class="wf-layout">
        <!-- LEFT: Workflow Cards -->
        <div class="wf-cards-col">
          <div id="cards">
            <?php foreach ($workflows as $idx => $wf): ?>
            <div class="card" id="card-<?= $wf['id'] ?>" onclick="onCardClick(<?= $wf['id'] ?>)" style="animation-delay:<?= $idx * 0.06 ?>s">
              <div class="card-icon" id="icon-<?= $wf['id'] ?>"><?= get_icon($wf['icon']) ?></div>
              <div class="card-text">
                <h3><?= htmlspecialchars($wf['title']) ?></h3>
                <p><?= htmlspecialchars($wf['subtitle']) ?></p>
              </div>
              <span class="progress-percent" id="percent-<?= $wf['id'] ?>">0%</span>
              <div class="progress-container" id="pbar-<?= $wf['id'] ?>">
                <div class="progress-fill" id="pfill-<?= $wf['id'] ?>"></div>
              </div>
            </div>
            <div class="rollout" id="rollout-<?= $wf['id'] ?>">
              <?php if ($wf['input_label']): ?>
              <div class="rollout-label"><?= htmlspecialchars($wf['input_label']) ?></div>
              <div class="rollout-input-row">
                <input class="rollout-input" id="input-<?= $wf['id'] ?>"
                       placeholder="<?= htmlspecialchars($wf['input_hint'] ?? '') ?>"
                       onkeydown="if(event.key==='Enter') confirmInput(<?= $wf['id'] ?>)">
                <button class="rollout-start" id="startbtn-<?= $wf['id'] ?>" onclick="confirmInput(<?= $wf['id'] ?>)">Starten</button>
              </div>
              <?php else: ?>
              <div class="rollout-noinput">
                <button class="rollout-start" id="startbtn-<?= $wf['id'] ?>" onclick="startWorkflow(<?= $wf['id'] ?>)">Starten</button>
              </div>
              <?php endif; ?>
              <div class="rollout-progress" id="rprogress-<?= $wf['id'] ?>"></div>
              <div class="rollout-result" id="rresult-<?= $wf['id'] ?>"></div>
              <div class="rollout-footer" id="rfooter-<?= $wf['id'] ?>">
                <button class="rollout-copy hidden" id="copybtn-<?= $wf['id'] ?>" onclick="copyLogContent(<?= $wf['id'] ?>)">📋 Kopieren</button>
                <button class="rollout-close" onclick="closeRollout(<?= $wf['id'] ?>)">Schliessen</button>
              </div>
            </div>
            <?php endforeach; ?>
          </div>
        </div>
        <!-- RIGHT: Script Output -->
        <div class="wf-log-col" id="logPanel">
          <div class="log-content" id="logContent"></div>
        </div>
      </div>
    </div>

    <!-- ═══ VIEW: HISTORY ═══ -->
    <div class="view" id="view-history">
      <div class="settings-section">
        <div class="settings-title">Verlauf</div>
        <div class="admin-list" id="historyList">
          <div class="update-empty">Lade...</div>
        </div>
      </div>
    </div>

    <!-- ═══ VIEW: SETTINGS ═══ -->
    <div class="view" id="view-settings">
      <!-- Settings Lock Overlay (nur für Non-Admins) -->
      <?php if ($user['role'] !== 'admin'): ?>
      <div class="settings-lock-overlay" id="settingsLockOverlay">
        <div class="settings-lock-panel">
          <div class="settings-lock-title">🔒 Geschützer Bereich</div>
          <p>Dieser Bereich ist nur für Administratoren zugänglich.</p>
          <form id="settingsPasswordForm" class="settings-password-form">
            <input type="password" id="settingsPassword" placeholder="Passwort eingeben" required>
            <button type="submit" class="submit-btn">Zugang</button>
          </form>
          <div id="settingsPasswordStatus"></div>
        </div>
      </div>
      <div class="settings-content-locked" id="settingsContentLocked"></div>
      <?php endif; ?>

      <div class="settings-content" id="settingsContent">
      <div class="settings-section">
        <div class="settings-title">Profil</div>
        <div id="userInfo">
          <div class="user-name"><?= htmlspecialchars($user['name']) ?></div>
          <div class="user-email"><?= htmlspecialchars($user['email']) ?></div>
          <div class="user-role-badge <?= $user['role'] ?>"><?= $user['role'] === 'admin' ? 'Admin' : 'User' ?></div>
        </div>
        <a href="/auth/logout.php" class="logout-btn" style="margin-top:12px">Abmelden</a>
      </div>

      <!-- BACKEND ACCESS -->
      <div class="settings-section">
        <div class="settings-title">Backend-Zugriff</div>
        <div id="backendAccess">
          <form id="adminAuthForm" class="admin-auth-form">
            <input type="password" id="adminPassword" placeholder="Passwort" required>
            <button type="submit" class="submit-btn">Anmelden</button>
          </form>
          <div id="adminAuthStatus"></div>
        </div>
      </div>

      <!-- SCRIPT EDITOR (visible after auth) -->
      <div id="scriptEditorPanel" class="hidden">
        <div class="settings-section">
          <div class="settings-title">Skripte bearbeiten</div>
          <div id="scriptsList" class="scripts-list"></div>
        </div>
        <div id="scriptEditorModal" class="hidden">
          <div class="editor-container">
            <div class="editor-header">
              <span id="editorTitle">Script Editor</span>
              <button type="button" class="close-btn" onclick="closeScriptEditor()">✕</button>
            </div>
            <textarea id="scriptContent" class="script-editor"></textarea>
            <div class="editor-footer">
              <button type="button" class="btn-save" onclick="saveScript()">Speichern</button>
              <button type="button" class="btn-cancel" onclick="closeScriptEditor()">Abbrechen</button>
            </div>
          </div>
        </div>
        <button type="button" class="logout-backend-btn" onclick="logoutBackend()" style="margin-top:12px">Backend Abmelden</button>
      </div>

      <?php if ($isAdmin): ?>
      <div id="adminPanel">
        <!-- SCRIPTS -->
        <div class="settings-section">
          <div class="settings-title">Skripte</div>
          <div class="admin-list" id="scriptsList"><div class="update-empty">Lade...</div></div>
        </div>

        <!-- USERS -->
        <div class="settings-section">
          <div class="settings-title">Benutzer</div>
          <div class="admin-list" id="usersList"><div class="update-empty">Lade...</div></div>
        </div>

        <!-- CONFIG -->
        <div class="settings-section">
          <div class="settings-title">Konfiguration</div>
          <div class="admin-list" id="configList"><div class="update-empty">Lade...</div></div>
        </div>

        <!-- LOGS -->
        <div class="settings-section">
          <div class="settings-title">Letzte Runs</div>
          <div class="admin-list" id="adminHistory"><div class="update-empty">Lade...</div></div>
        </div>
      </div>
      <?php endif; ?>
      </div><!-- closes settings-content -->
    </div>

    <!-- STATUS BAR -->
    <div class="status-bar">
      <span class="status-label">Status:</span>
      <span class="status-text" id="statusText">Bereit</span>
    </div>

  </div>

</div>

<script>
// Pass user data to JS
const CURRENT_USER = <?= json_encode($user) ?>;
const IS_ADMIN = <?= $isAdmin ? 'true' : 'false' ?>;
const WORKFLOWS = <?= json_encode($workflows) ?>;
</script>
<script src="/postpro/assets/js/app.js"></script>
</body>
</html>

<?php
function get_icon(string $name): string {
    $map = [
        'arrow-down-circle' => '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="var(--blue)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14M5 12l7 7 7-7"/></svg>',
        'folder-open'       => '&#128194;',
        'folder'            => '&#128193;',
        'check-square'      => '&#127915;',
        'cloud-upload'      => '&#9729;',
        'image'             => '&#128444;',
        'trash-2'           => '&#128465;',
    ];
    return $map[$name] ?? '&#9654;';
}
?>
