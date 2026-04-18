const express = require('express');
const cors = require('cors');
const path = require('path');
const session = require('express-session');
const app = express();

// Middleware
app.use(cors());
app.use(express.json());
app.use(express.urlencoded({ extended: true }));
app.use(session({
  secret: 'PostProSuite2026!',
  resave: false,
  saveUninitialized: true,
  cookie: { secure: false, maxAge: 1000 * 60 * 60 * 24 }
}));

// Statische Dateien
app.use(express.static(path.join(__dirname, 'public')));

// ═══ SIMPLE AUTH SIMULATION ═══
const VALID_USER = {
  email: 'admin@postpro.local',
  name: 'Admin',
  role: 'admin'
};

// ═══ ROUTES ═══

// Login Check
app.get('/api/user', (req, res) => {
  if (req.session.user) {
    res.json(req.session.user);
  } else {
    res.status(401).json({ error: 'Not authenticated' });
  }
});

// Fake Login
app.post('/api/login', (req, res) => {
  req.session.user = VALID_USER;
  res.json({ success: true, user: VALID_USER });
});

// Main Dashboard
app.get('/', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'dashboard.html'));
});

// Fallback für alle anderen Routes (SPA)
app.use((req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'dashboard.html'));
});

module.exports = app;
