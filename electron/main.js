const { app, BrowserWindow, Menu, shell } = require("electron");
const path = require("path");
const fs = require("fs");

const DEFAULT_URL = "https://termin.solids.group";
const WINDOW_STATE_FILE = "window-state.json";

function appDataFile(name) {
  return path.join(app.getPath("userData"), name);
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
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true
    }
  });

  buildMenu(mainWindow);

  mainWindow.once("ready-to-show", () => {
    mainWindow.show();
  });

  mainWindow.on("close", () => persistWindowState(mainWindow));
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });
  mainWindow.webContents.on("will-navigate", (event, url) => {
    const baseUrl = getBaseUrl();
    if (!url.startsWith(baseUrl)) {
      event.preventDefault();
      shell.openExternal(url);
    }
  });

  mainWindow.loadURL(getBaseUrl());
}

app.whenReady().then(() => {
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
