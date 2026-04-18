<?php
/**
 * PostPro Suite — Login (Patel-Style, LUQOM-branded)
 * - Animated Toggle zwischen Sign-In und "Request Access"
 * - LUQOM Brand-Palette + Montserrat
 * - Splash-Intro mit LUQOM Schriftzug + Auslöser-Blitz
 */
?>
<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PostPro Suite — Login</title>
<link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box;font-family:'Montserrat',sans-serif;}

:root{
  --luqom-black:#20232e;
  --luqom-gray:#5d606e;
  --luqom-light:#ebebeb;
  --luqom-blue:#3a77a8;
  --luqom-blue-dark:#2a5f8f;
}

body{
  background:linear-gradient(135deg,var(--luqom-black) 0%,var(--luqom-gray) 100%);
  display:flex;align-items:center;justify-content:center;
  flex-direction:column;height:100vh;overflow:hidden;
  position:relative;
}
body::before,body::after{
  content:'';position:absolute;border-radius:50%;
  background:radial-gradient(circle,rgba(58,119,168,0.25),transparent 70%);
  pointer-events:none;
}
body::before{width:600px;height:600px;top:-200px;left:-200px;}
body::after{width:500px;height:500px;bottom:-150px;right:-150px;}

/* ═══ SPLASH ═══ */
.splash{
  position:fixed;inset:0;
  background:linear-gradient(135deg,var(--luqom-black) 0%,var(--luqom-gray) 100%);
  display:flex;align-items:center;justify-content:center;
  z-index:1000;transition:opacity 0.6s ease;
  overflow:hidden;
}
.splash.fade-out{opacity:0;pointer-events:none;}

.splash-inner{
  position:relative;
  width:400px;height:220px;
  display:flex;align-items:center;justify-content:center;
}

/* Logo mit Schriftzug — erscheint und wächst auf */
.splash-logo{
  position:absolute;
  opacity:0;
  animation: logoScale 0.8s cubic-bezier(0.34,1.56,0.64,1) 0.2s forwards;
  filter:drop-shadow(0 0 40px rgba(58,119,168,0.4));
}
@keyframes logoScale{
  from{opacity:0;transform:scale(0.7);}
  to{opacity:1;transform:scale(1);}
}

/* Auslöser-Blitz (Shutter Flash) */
.splash-flash{
  position:fixed;inset:0;
  background:#fff;
  opacity:0;pointer-events:none;
  z-index:1001;
  animation: shutterFlash 0.45s ease 1.15s 1;
}
@keyframes shutterFlash{
  0%  {opacity:0;}
  25% {opacity:0.92;}
  60% {opacity:0.65;}
  100%{opacity:0;}
}

/* ═══ CONTAINER ═══ */
.container{
  background:#fff;
  border-radius:30px;
  box-shadow:0 20px 60px rgba(0,0,0,0.4),0 0 0 1px rgba(255,255,255,0.08);
  position:relative;overflow:hidden;
  width:820px;max-width:92%;min-height:520px;
  opacity:0;
  animation:cardIn 0.9s cubic-bezier(0.4,0,0.2,1) 1.8s forwards;
  z-index:1;
}
@keyframes cardIn{
  from{opacity:0;transform:translateY(30px) scale(0.96);}
  to{opacity:1;transform:translateY(0) scale(1);}
}

/* ═══ FORM PANELS ═══ */
.container p{font-size:13px;line-height:20px;letter-spacing:0.3px;margin:14px 0;color:var(--luqom-gray);}
.container span{font-size:12px;color:var(--luqom-gray);}
.container a{color:var(--luqom-gray);font-size:12px;text-decoration:none;margin:14px 0 8px;}
.container a:hover{color:var(--luqom-blue);}

.container button{
  background:var(--luqom-blue);color:#fff;
  font-size:12px;padding:11px 45px;
  border:1px solid transparent;border-radius:8px;
  font-weight:600;letter-spacing:0.8px;text-transform:uppercase;
  margin-top:12px;cursor:pointer;
  box-shadow:0 4px 14px rgba(58,119,168,0.35);
  transition:background 0.2s,transform 0.15s,box-shadow 0.2s;
}
.container button:hover{background:var(--luqom-blue-dark);transform:translateY(-1px);box-shadow:0 6px 18px rgba(58,119,168,0.45);}
.container button:active{transform:translateY(0);}
.container button.ghost{
  background:transparent;border-color:#fff;color:#fff;
  box-shadow:none;
}
.container button.ghost:hover{background:rgba(255,255,255,0.1);}

.container form{
  background:#fff;
  display:flex;align-items:center;justify-content:center;
  flex-direction:column;padding:0 44px;height:100%;
  text-align:center;
}
.container h1{
  font-size:26px;font-weight:700;color:var(--luqom-black);
  letter-spacing:-0.3px;margin-bottom:4px;
}
.container input{
  background:var(--luqom-light);border:1px solid transparent;
  margin:7px 0;padding:12px 16px;
  font-size:13px;border-radius:10px;width:100%;outline:none;
  color:var(--luqom-black);
  transition:border-color 0.2s,background 0.2s;
}
.container input::placeholder{color:var(--luqom-gray);}
.container input:focus{
  background:#fff;border-color:var(--luqom-blue);
  box-shadow:0 0 0 3px rgba(58,119,168,0.12);
}

.form-container{
  position:absolute;top:0;height:100%;
  transition:all 0.6s ease-in-out;
}
.sign-in{left:0;width:50%;z-index:2;}
.container.active .sign-in{transform:translateX(100%);}
.sign-up{left:0;width:50%;opacity:0;z-index:1;}
.container.active .sign-up{
  transform:translateX(100%);opacity:1;z-index:5;
  animation:move 0.6s;
}
@keyframes move{
  0%,49.99%{opacity:0;z-index:1;}
  50%,100%{opacity:1;z-index:5;}
}

.form-error{
  width:100%;padding:10px 14px;margin-bottom:10px;
  background:rgba(255,69,58,0.12);
  border:1px solid rgba(255,69,58,0.3);
  border-radius:8px;
  color:#d9302b;font-size:12px;
}
.form-note{
  width:100%;padding:10px 14px;margin:6px 0 4px;
  background:rgba(58,119,168,0.08);
  border:1px solid rgba(58,119,168,0.22);
  border-radius:8px;
  color:var(--luqom-blue-dark);font-size:11px;line-height:1.45;
}

.brand-mark{
  width:54px;height:54px;margin-bottom:12px;
  filter:drop-shadow(0 2px 10px rgba(58,119,168,0.2));
}
.brand-mark svg{width:100%;height:100%;}
.brand-mark svg path{fill:var(--luqom-blue);}

.version{
  margin-top:18px;font-size:10px;
  color:var(--luqom-gray);letter-spacing:1.5px;
  font-family:"SF Mono",Menlo,monospace;
}

/* ═══ TOGGLE PANEL ═══ */
.toggle-container{
  position:absolute;top:0;left:50%;width:50%;height:100%;
  overflow:hidden;
  transition:all 0.6s ease-in-out;
  border-radius:150px 0 0 150px;
  z-index:1000;
}
.container.active .toggle-container{
  transform:translateX(-100%);
  border-radius:0 150px 150px 0;
}
.toggle{
  background:linear-gradient(135deg,var(--luqom-blue) 0%,var(--luqom-blue-dark) 100%);
  height:100%;color:#fff;position:relative;left:-100%;width:200%;
  transform:translateX(0);
  transition:all 0.6s ease-in-out;
}
.toggle::before{
  content:'';position:absolute;
  width:400px;height:400px;border-radius:50%;
  background:radial-gradient(circle,rgba(255,255,255,0.08),transparent 70%);
  top:-100px;right:-100px;pointer-events:none;
}
.container.active .toggle{transform:translateX(50%);}

.toggle-panel{
  position:absolute;width:50%;height:100%;
  display:flex;align-items:center;justify-content:center;
  flex-direction:column;padding:0 36px;text-align:center;top:0;
  transform:translateX(0);
  transition:all 0.6s ease-in-out;
}
.toggle-left{transform:translateX(-200%);}
.container.active .toggle-left{transform:translateX(0);}
.toggle-right{right:0;transform:translateX(0);}
.container.active .toggle-right{transform:translateX(200%);}

.toggle-panel .brand-icon{
  width:80px;height:80px;margin-bottom:18px;
  filter:drop-shadow(0 4px 20px rgba(0,0,0,0.2));
}
.toggle-panel .brand-icon svg{width:100%;height:100%;}
.toggle-panel h1{font-size:22px;font-weight:700;letter-spacing:-0.2px;margin-bottom:8px;color:#fff;}
.toggle-panel p{color:rgba(255,255,255,0.85);margin:6px 0 18px;max-width:260px;}
.toggle-panel .toggle-wordmark{width:110px;margin-top:28px;opacity:0.9;}

@media (max-width:720px){
  .container{min-height:auto;width:92%;}
}
</style>
</head>
<body>

<!-- ═══ SPLASH ═══ -->
<div class="splash" id="splash">
  <div class="splash-flash"></div>
  <div class="splash-inner">
    <div class="splash-logo">
      <img src="/postpro/assets/logo/LUQOM.svg" alt="LUQOM GROUP" style="width:280px;height:auto;">
    </div>
  </div>
</div>

<!-- ═══ CONTAINER ═══ -->
<div class="container" id="container">

  <!-- SIGN-UP / Request Access -->
  <div class="form-container sign-up">
    <form method="POST" action="/postpro/auth/request-access.php">
      <h1>Request Access</h1>
      <span>Noch kein Account? IT-Zugang anfragen</span>
      <input type="text" name="name" placeholder="Name" required>
      <input type="email" name="email" placeholder="E-Mail (@lampenwelt.de / @luqom.com)" required>
      <input type="text" name="team" placeholder="Team / Abteilung" required>
      <div class="form-note">Deine Anfrage geht an IT. Account wird per SSO bereitgestellt.</div>
      <button type="submit">Anfrage senden</button>
    </form>
  </div>

  <!-- SIGN-IN -->
  <div class="form-container sign-in">
    <form method="POST" action="/postpro/auth/login.php">
      <h1>Sign In</h1>
      <span>Mit deiner Firmen-E-Mail anmelden</span>

      <?php if (!empty($loginError)): ?>
        <div class="form-error"><?= htmlspecialchars($loginError) ?></div>
      <?php endif; ?>
      <?php
        $accessMsg = $_GET['access'] ?? '';
        if ($accessMsg === 'ok'):      ?><div class="form-note">Deine Zugangsanfrage wurde an IT geschickt. Du hörst bald von uns.</div><?php
        elseif ($accessMsg === 'invalid'): ?><div class="form-error">Bitte alle Felder ausfüllen mit gültiger E-Mail.</div><?php
        elseif ($accessMsg === 'domain'):  ?><div class="form-error">Nur @lampenwelt.de oder @luqom.com E-Mails sind zugelassen.</div><?php
        endif;
      ?>

      <input type="email" name="email" placeholder="E-Mail Address" required autofocus>
      <a href="mailto:it@luqom.com?subject=PostPro Suite - Passwort vergessen">Passwort vergessen?</a>
      <button type="submit">Sign In</button>
      <div class="version">v<?= APP_VERSION ?></div>
    </form>
  </div>

  <!-- TOGGLE PANEL -->
  <div class="toggle-container">
    <div class="toggle">

      <!-- LEFT: zeigt sich, wenn Container.active (Sign-Up aktiv) → CTA "zurueck zum Sign-In" -->
      <div class="toggle-panel toggle-left">
        <h1>Willkommen zurück!</h1>
        <p>Du hast schon einen Account? Melde dich an und leg mit deinen Workflows los.</p>
        <button class="ghost" id="login" type="button">Sign In</button>
      </div>

      <!-- RIGHT: Default-Ansicht → CTA "Access anfragen" -->
      <div class="toggle-panel toggle-right">
        <div class="brand-icon">
          <img src="/postpro/assets/logo/LUQOM-Icon.svg" alt="LUQOM" style="width:100%;height:100%">
        </div>
        <h1>PostPro Suite</h1>
        <p>DAM Downloads, Image Classification &amp; Jira-getriebene Produktions-Workflows.</p>
        <button class="ghost" id="register" type="button">Request Access</button>
      </div>

    </div>
  </div>
</div>

<script>
const container = document.getElementById('container');
const registerBtn = document.getElementById('register');
const loginBtn = document.getElementById('login');

registerBtn.addEventListener('click', () => container.classList.add('active'));
loginBtn.addEventListener('click',    () => container.classList.remove('active'));

// Splash-Animation: Logo (0.2s start, 0.8s duration) → Shutter Flash (1.15s) → Login Form (1.8s)
setTimeout(() => document.getElementById('splash').classList.add('fade-out'), 1.8);
setTimeout(() => { const s = document.getElementById('splash'); if (s) s.style.display = 'none'; }, 2.4);
</script>
</body>
</html>
