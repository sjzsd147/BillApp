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
  exportLogs: (options = {}) => ipcRenderer.invoke('bill:export-logs', options),
  getDesktopInfo: () => ipcRenderer.invoke('bill:get-desktop-info'),
  updater: {
    getState: () => ipcRenderer.invoke('bill:updater-get-state'),
    checkForUpdates: () => ipcRenderer.invoke('bill:updater-check'),
    downloadUpdate: () => ipcRenderer.invoke('bill:updater-download'),
    quitAndInstall: () => ipcRenderer.invoke('bill:updater-quit-and-install'),
    openReleasePage: () => ipcRenderer.invoke('bill:updater-open-release'),
    onStateChange: (callback) => {
      if (typeof callback !== 'function') {
        return () => {};
      }
      const listener = (_event, state) => callback(state);
      ipcRenderer.on('bill:updater-state', listener);
      return () => {
        ipcRenderer.removeListener('bill:updater-state', listener);
      };
    },
  },
});
