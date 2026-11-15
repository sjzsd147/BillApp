'use strict';

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('billDesktop', {
  isElectron: true,
  platform: process.platform,
  versions: {
    electron: process.versions.electron,
    node: process.versions.node,
    chrome: process.versions.chrome,
  },
  saveDatabaseBackup: (defaultFilename = '') =>
    ipcRenderer.invoke('bill:save-database-backup', { defaultFilename }),
  getDesktopInfo: () => ipcRenderer.invoke('bill:get-desktop-info'),
});
