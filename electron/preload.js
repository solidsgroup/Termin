const { contextBridge } = require("electron");

contextBridge.exposeInMainWorld("terminDesktop", {
  isDesktopApp: true
});
