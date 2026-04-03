const { app, BrowserWindow, Menu, nativeImage, shell } = require("electron");
const path = require("path");
const fs = require("fs");

const DEFAULT_URL = "https://termin.solids.group";
const WINDOW_STATE_FILE = "window-state.json";
const AUTH_START_PATHS = new Set([
  "/login/google",
  "/login/github",
  "/login/microsoft",
  "/connect/google",
  "/connect/github",
  "/connect/microsoft"
]);
const AUTH_CALLBACK_PATHS = new Set([
  "/auth/google/callback",
  "/auth/github/callback",
  "/auth/microsoft/callback"
]);

// Chromium sandbox regularly blocks Linux desktop launches in user environments.
app.commandLine.appendSwitch("no-sandbox");
app.commandLine.appendSwitch("disable-gpu-sandbox");
if (process.platform === "linux") {
  app.commandLine.appendSwitch("class", "Termin");
}

function appDataFile(name) {
  return path.join(app.getPath("userData"), name);
}

function getIconPath() {
  const generatedPng = path.join(__dirname, "build", "icons", "icon_512x512.png");
  if (fs.existsSync(generatedPng)) {
    return generatedPng;
  }
  return path.join(__dirname, "assets", "icon.svg");
}

function getAppIcon() {
  return nativeImage.createFromPath(getIconPath());
}

function getBaseOrigin() {
  return new URL(getBaseUrl()).origin;
}

function parseUrl(rawUrl) {
  try {
    return new URL(rawUrl);
  } catch (error) {
    return null;
  }
}

function isBaseAppUrl(rawUrl) {
  const parsed = parseUrl(rawUrl);
  return !!parsed && parsed.origin === getBaseOrigin();
}

function isAuthStartUrl(rawUrl) {
  const parsed = parseUrl(rawUrl);
  return !!parsed && parsed.origin === getBaseOrigin() && AUTH_START_PATHS.has(parsed.pathname);
}

function isAuthCompletionUrl(rawUrl) {
  const parsed = parseUrl(rawUrl);
  if (!parsed || parsed.origin !== getBaseOrigin()) {
    return false;
  }
  return !AUTH_START_PATHS.has(parsed.pathname) && !AUTH_CALLBACK_PATHS.has(parsed.pathname);
}

function getBaseUrl() {
  return (
    process.env.TERMIN_DESKTOP_URL ||
    process.env.PUBLIC_BASE_URL ||
    DEFAULT_URL
  ).replace(/\/+$/, "");
}

function readWindowState() {
  const fallback = { width: 1440, height: 920 };
  try {
    const raw = fs.readFileSync(appDataFile(WINDOW_STATE_FILE), "utf8");
    const state = JSON.parse(raw);
    if (
      Number.isFinite(state.width) &&
      Number.isFinite(state.height) &&
      state.width > 0 &&
      state.height > 0
    ) {
      return { ...fallback, ...state };
    }
  } catch (error) {
    return fallback;
  }
  return fallback;
}

function persistWindowState(window) {
  if (window.isDestroyed()) {
    return;
  }
  const bounds = window.getBounds();
  fs.mkdirSync(app.getPath("userData"), { recursive: true });
  fs.writeFileSync(appDataFile(WINDOW_STATE_FILE), JSON.stringify(bounds, null, 2));
}

function buildMenu(mainWindow) {
  const template = [
    {
      label: "Termin",
      submenu: [
        { role: "about" },
        { type: "separator" },
        {
          label: "Open In Browser",
          click: () => shell.openExternal(getBaseUrl())
        },
        { type: "separator" },
        { role: "quit" }
      ]
    },
    {
      label: "View",
      submenu: [
        { role: "reload" },
        { role: "forceReload" },
        { role: "togglefullscreen" },
        {
          label: "Toggle Developer Tools",
          accelerator: "CmdOrCtrl+Shift+I",
          click: () => mainWindow.webContents.toggleDevTools()
        }
      ]
    },
    {
      label: "Window",
      submenu: [{ role: "minimize" }, { role: "zoom" }, { role: "close" }]
    }
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

function openAuthWindow(mainWindow, authUrl) {
  const authWindow = new BrowserWindow({
    parent: mainWindow,
    modal: false,
    width: 1180,
    height: 860,
    minWidth: 960,
    minHeight: 720,
    autoHideMenuBar: true,
    show: false,
    title: "Termin Sign In",
    backgroundColor: "#111827",
    icon: getAppIcon(),
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false
    }
  });

  let sawAuthCallback = false;

  const maybeFinishAuth = (targetUrl) => {
    const parsed = parseUrl(targetUrl);
    if (!parsed || parsed.origin !== getBaseOrigin()) {
      return;
    }
    if (AUTH_CALLBACK_PATHS.has(parsed.pathname)) {
      sawAuthCallback = true;
      return;
    }
    if (!sawAuthCallback || !isAuthCompletionUrl(targetUrl)) {
      return;
    }
    mainWindow.loadURL(targetUrl);
    if (!authWindow.isDestroyed()) {
      authWindow.close();
    }
  };

  authWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });
  authWindow.webContents.on("will-redirect", (_event, targetUrl) => {
    maybeFinishAuth(targetUrl);
  });
  authWindow.webContents.on("did-navigate", (_event, targetUrl) => {
    maybeFinishAuth(targetUrl);
  });
  authWindow.on("closed", () => {
    if (!mainWindow.isDestroyed()) {
      mainWindow.focus();
    }
  });
  authWindow.once("ready-to-show", () => {
    authWindow.show();
  });
  authWindow.loadURL(authUrl);
}

function createWindow() {
  const state = readWindowState();
  const mainWindow = new BrowserWindow({
    width: state.width,
    height: state.height,
    x: state.x,
    y: state.y,
    minWidth: 1100,
    minHeight: 720,
    autoHideMenuBar: false,
    show: false,
    title: "Termin",
    backgroundColor: "#111827",
    icon: getAppIcon(),
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false
    }
  });

  buildMenu(mainWindow);
  if (process.platform === "linux") {
    mainWindow.setIcon(getAppIcon());
  }

  mainWindow.once("ready-to-show", () => {
    mainWindow.show();
  });

  mainWindow.on("close", () => persistWindowState(mainWindow));
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (isAuthStartUrl(url)) {
      openAuthWindow(mainWindow, url);
      return { action: "deny" };
    }
    shell.openExternal(url);
    return { action: "deny" };
  });
  mainWindow.webContents.on("will-navigate", (event, url) => {
    if (isAuthStartUrl(url)) {
      event.preventDefault();
      openAuthWindow(mainWindow, url);
      return;
    }
    if (!isBaseAppUrl(url)) {
      event.preventDefault();
      shell.openExternal(url);
    }
  });

  mainWindow.loadURL(getBaseUrl());
}

app.whenReady().then(() => {
  app.setName("Termin");
  if (process.platform === "darwin" && app.dock) {
    app.dock.setIcon(getIconPath());
  }
  createWindow();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit();
  }
});
