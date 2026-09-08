// Agent Capital — macOS desktop wrapper.
//
// On launch it looks for the Flask backend on http://localhost:$AGENT_CAPITAL_PORT
// (default 8888). If it is already running (e.g. via launchd or a terminal) the
// window simply loads it. If not, it spawns `<repo>/venv/bin/python backend/server.py`
// itself, waits for it to answer, and kills that child again on quit — only the
// child it spawned, never a pre-existing server.

const { app, BrowserWindow, Menu, shell } = require('electron');
const { spawn } = require('child_process');
const fs = require('fs');
const http = require('http');
const os = require('os');
const path = require('path');

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

// Absolute repo path baked in for the packaged app (personal single-machine app).
const DEFAULT_REPO_ROOT = '/Users/charvipattila/code/my-claude/agent-capitol';
const CONFIG_PATH = path.join(os.homedir(), '.agent-capital', 'desktop.json');

function readUserConfig() {
  try {
    return JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf8'));
  } catch (_) {
    return {};
  }
}

function looksLikeRepo(dir) {
  return dir && fs.existsSync(path.join(dir, 'backend', 'server.py'));
}

function resolveRepoRoot(cfg) {
  const candidates = [
    process.env.AGENT_CAPITAL_ROOT,
    cfg.root,
    app.isPackaged ? null : path.resolve(__dirname, '..'), // `npm start` from desktop/
    DEFAULT_REPO_ROOT,
  ];
  for (const c of candidates) if (looksLikeRepo(c)) return c;
  return DEFAULT_REPO_ROOT;
}

const userConfig = readUserConfig();
const REPO_ROOT = resolveRepoRoot(userConfig);
const PORT = String(process.env.AGENT_CAPITAL_PORT || userConfig.port || 8888);
const SERVER_URL = `http://localhost:${PORT}`;
const PYTHON = process.env.AGENT_CAPITAL_PYTHON || userConfig.python || path.join(REPO_ROOT, 'venv', 'bin', 'python');
const SERVER_SCRIPT = process.env.AGENT_CAPITAL_SERVER || userConfig.server || path.join(REPO_ROOT, 'backend', 'server.py');
const LOG_DIR = path.join(os.homedir(), '.agent-capital', 'logs');
const LOG_PATH = path.join(LOG_DIR, 'desktop-backend.log');
const STARTUP_TIMEOUT_MS = 20000;
const BG_COLOR = '#0d0d11'; // matches --bg in frontend/src/App.css

// ---------------------------------------------------------------------------
// Window state (bounds remembered between launches)
// ---------------------------------------------------------------------------

const STATE_PATH = path.join(app.getPath('userData'), 'window-state.json');

function loadWindowState() {
  try {
    const s = JSON.parse(fs.readFileSync(STATE_PATH, 'utf8'));
    if (Number.isFinite(s.width) && Number.isFinite(s.height)) return s;
  } catch (_) {}
  return { width: 1280, height: 860 };
}

function saveWindowState(win) {
  try {
    const bounds = win.isMaximized() ? win.getNormalBounds() : win.getBounds();
    fs.writeFileSync(STATE_PATH, JSON.stringify({ ...bounds, maximized: win.isMaximized() }));
  } catch (_) {}
}

// ---------------------------------------------------------------------------
// Backend probing / spawning
// ---------------------------------------------------------------------------

function probe(url, timeoutMs = 1500) {
  return new Promise((resolve) => {
    const req = http.get(url, (res) => {
      res.resume();
      resolve(res.statusCode > 0 && res.statusCode < 500);
    });
    req.setTimeout(timeoutMs, () => { req.destroy(); resolve(false); });
    req.on('error', () => resolve(false));
  });
}

let child = null; // backend process spawned by this app, if any
let childExit = null; // { code, signal } once it exits

function spawnBackend() {
  fs.mkdirSync(LOG_DIR, { recursive: true });
  const logFd = fs.openSync(LOG_PATH, 'a');
  fs.writeSync(logFd, `\n===== ${new Date().toISOString()} spawn ${PYTHON} ${SERVER_SCRIPT} (PORT=${PORT}) =====\n`);
  const env = {
    HOME: os.homedir(),
    USER: process.env.USER || os.userInfo().username,
    LANG: process.env.LANG || 'en_US.UTF-8',
    PATH: `${os.homedir()}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin`,
    PORT,
  };
  child = spawn(PYTHON, [SERVER_SCRIPT], {
    cwd: REPO_ROOT,
    env,
    stdio: ['ignore', logFd, logFd],
    detached: false,
  });
  child.on('exit', (code, signal) => { childExit = { code, signal }; });
  child.on('error', (err) => {
    childExit = { code: -1, signal: null, error: err.message };
    fs.writeSync(logFd, `spawn error: ${err.message}\n`);
  });
}

function stopBackend() {
  if (child && !childExit) {
    try { child.kill('SIGTERM'); } catch (_) {}
    child = null;
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitForBackend(onStatus) {
  const started = Date.now();
  while (Date.now() - started < STARTUP_TIMEOUT_MS) {
    if (childExit) return false; // process died early; no point waiting
    if (await probe(SERVER_URL, 1000)) return true;
    onStatus(`Waiting for ${SERVER_URL} (${Math.round((Date.now() - started) / 1000)}s)`);
    await sleep(500);
  }
  return false;
}

// ---------------------------------------------------------------------------
// Window
// ---------------------------------------------------------------------------

let mainWindow = null;

function setLoadingText(win, title, sub) {
  const js = `(function(){var t=document.getElementById('title');var s=document.getElementById('sub');
    if(t)t.textContent=${JSON.stringify(title)};if(s)s.textContent=${JSON.stringify(sub || '')};})();`;
  win.webContents.executeJavaScript(js).catch(() => {});
}

function showStartupError(win, message) {
  const tail = (() => {
    try {
      const data = fs.readFileSync(LOG_PATH, 'utf8');
      return data.split('\n').slice(-25).join('\n');
    } catch (_) { return '(no log yet)'; }
  })();
  const js = `(function(){var b=document.getElementById('box');b.className='box err';
    b.innerHTML='<div class="title">Agent Capital could not start</div>'
      +'<div class="sub"></div><pre></pre><button id="retry">Retry (⌘R)</button>';
    b.querySelector('.sub').textContent=${JSON.stringify(`${message}\nLog: ${LOG_PATH}`)};
    b.querySelector('pre').textContent=${JSON.stringify(tail)};
    document.getElementById('retry').onclick=function(){document.title='agent-capital:retry';};})();`;
  win.webContents.executeJavaScript(js).catch(() => {});
}

function createWindow() {
  const state = loadWindowState();
  const win = new BrowserWindow({
    width: state.width,
    height: state.height,
    x: state.x,
    y: state.y,
    minWidth: 720,
    minHeight: 480,
    show: false,
    backgroundColor: BG_COLOR,
    title: 'Agent Capital',
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      spellcheck: false,
    },
  });
  mainWindow = win;
  if (state.maximized) win.maximize();

  // Open external links (target=_blank or a different origin) in the system browser.
  win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });
  win.webContents.on('will-navigate', (event, url) => {
    if (!url.startsWith(SERVER_URL + '/') && url !== SERVER_URL) {
      event.preventDefault();
      shell.openExternal(url);
    }
  });

  // The loading page's Retry button signals us via the document title (no preload needed).
  win.webContents.on('page-title-updated', (event, title) => {
    if (title === 'agent-capital:retry') { event.preventDefault(); boot(win); }
  });

  win.once('ready-to-show', () => win.show());
  const persist = () => saveWindowState(win);
  win.on('resize', persist);
  win.on('move', persist);
  win.on('close', persist);
  win.on('closed', () => { mainWindow = null; });
  return win;
}

// Debug aid: AGENT_CAPITAL_SCREENSHOT=/path/out.png writes a capture of the
// rendered page ~3s after the UI loads (used for automated UI checks).
function debugScreenshot(win) {
  const out = process.env.AGENT_CAPITAL_SCREENSHOT;
  if (!out) return;
  setTimeout(async () => {
    try {
      const img = await win.webContents.capturePage();
      fs.writeFileSync(out, img.toPNG());
    } catch (err) {
      fs.writeFileSync(out + '.err', String(err));
    }
  }, 3000);
}

async function boot(win) {
  await win.loadFile(path.join(__dirname, 'loading.html'));

  if (await probe(SERVER_URL)) {
    await win.loadURL(SERVER_URL);
    debugScreenshot(win);
    return;
  }

  // Loading page with the reload-to-retry hook: reloading re-runs boot().
  setLoadingText(win, 'Starting Agent Capital…', `Launching backend on port ${PORT}`);

  if (!fs.existsSync(PYTHON) || !fs.existsSync(SERVER_SCRIPT)) {
    showStartupError(win, `Backend not found.\npython: ${PYTHON}\nserver: ${SERVER_SCRIPT}\n` +
      `Set AGENT_CAPITAL_ROOT or "root" in ${CONFIG_PATH}.`);
    return;
  }

  if (!child || childExit) { childExit = null; spawnBackend(); }
  const ok = await waitForBackend((s) => setLoadingText(win, 'Starting Agent Capital…', s));
  if (ok) {
    await win.loadURL(SERVER_URL);
    debugScreenshot(win);
  } else {
    const why = childExit
      ? `Backend exited early (code ${childExit.code}${childExit.signal ? ', signal ' + childExit.signal : ''}${childExit.error ? ': ' + childExit.error : ''}).`
      : `Backend did not answer on ${SERVER_URL} within ${STARTUP_TIMEOUT_MS / 1000}s.`;
    showStartupError(win, why);
  }
}

// ---------------------------------------------------------------------------
// Menu
// ---------------------------------------------------------------------------

function buildMenu() {
  const template = [
    {
      label: app.name,
      submenu: [
        { role: 'about' },
        { type: 'separator' },
        {
          label: 'Open in Browser',
          click: () => shell.openExternal(SERVER_URL),
        },
        { type: 'separator' },
        { role: 'hide' },
        { role: 'hideOthers' },
        { role: 'unhide' },
        { type: 'separator' },
        { role: 'quit' },
      ],
    },
    { role: 'editMenu' },
    {
      label: 'View',
      submenu: [
        {
          label: 'Reload',
          accelerator: 'CmdOrCtrl+R',
          click: () => {
            const win = BrowserWindow.getFocusedWindow() || mainWindow;
            if (!win) return;
            // If the backend was never reached, reload re-runs the boot sequence.
            if (win.webContents.getURL().startsWith('file:')) boot(win); else win.reload();
          },
        },
        { role: 'forceReload' },
        { role: 'toggleDevTools' },
        { type: 'separator' },
        { role: 'resetZoom' },
        { role: 'zoomIn' },
        { role: 'zoomOut' },
        { type: 'separator' },
        { role: 'togglefullscreen' },
      ],
    },
    { role: 'windowMenu' },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

// ---------------------------------------------------------------------------
// App lifecycle
// ---------------------------------------------------------------------------

app.setName('Agent Capital');

app.whenReady().then(() => {
  buildMenu();
  boot(createWindow());

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) boot(createWindow());
  });
});

app.on('window-all-closed', () => {
  // macOS convention: stay in the Dock; the backend keeps running until Quit.
});

app.on('before-quit', () => stopBackend());
app.on('will-quit', () => stopBackend());
process.on('exit', () => stopBackend());
