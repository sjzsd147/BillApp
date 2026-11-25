'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { app, BrowserWindow, Menu, dialog, ipcMain, net, safeStorage, session, shell } = require('electron');
const { startBackend, httpPostBuffer, httpPostJsonForBuffer, parseDbCliArg } = require('./backend.cjs');
const { createDesktopLogger } = require('./logger.cjs');
const { createAppUpdater } = require('./updater.cjs');

app.setName('BillApp');
const desktopLogger = createDesktopLogger({
  logDir: app.getPath('userData'),
  filename: 'desktop.log',
  mirrorConsole: !app.isPackaged && !process.argv.includes('--smoke-test'),
});
// Ensure loopback traffic to the local FastAPI sidecar never routes through system HTTP proxies.
app.commandLine.appendSwitch('proxy-bypass-list', '<-loopback>,127.0.0.1,localhost');

const WINDOW_TITLE = '账单助手 - BillApp';
const SMOKE_TEST =
  process.argv.includes('--smoke-test') || (!app.isPackaged && process.env.BILLAPP_ELECTRON_SMOKETEST === '1');
const ALLOW_MULTI =
  SMOKE_TEST || (!app.isPackaged && process.env.BILLAPP_ALLOW_MULTI_INSTANCE === '1');

let mainWindow = null;
let backendHandle = null;
let isQuitting = false;
let startInFlight = null;
let stopInFlight = null;
let appUpdaterController = null;
let trustedOrigins = [];
let sessionSecurityConfigured = false;
let downloadListenerRegistered = false;
let hasSingleInstanceLock = true;

if (!ALLOW_MULTI) {
  const gotLock = app.requestSingleInstanceLock();
  if (!gotLock) {
    hasSingleInstanceLock = false;
    app.quit();
  }
}

function getRepoRoot() {
  return path.resolve(__dirname, '..', '..');
}

function resolveFrontendDistDir() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, 'frontend-dist');
  }
  return path.join(getRepoRoot(), 'frontend', 'dist');
}

function resolveIconPath() {
  const candidates = [
    path.join(__dirname, '..', 'assets', 'icon.png'),
    path.join(process.resourcesPath || '', 'assets', 'icon.png'),
  ];
  for (const p of candidates) {
    if (p && fs.existsSync(p)) {
      return p;
    }
  }
  return undefined;
}

function sanitizeDownloadFilename(rawName, fallback = 'billapp-export.dat') {
  const base = path.basename(String(rawName || '').trim()).replace(/[\r\n\0<>:"/\\|?*]+/g, '_');
  if (!base || base === '.' || base === '..') {
    return fallback;
  }
  return base;
}

function getOrCreateSafeStorageMasterKey() {
  try {
    if (!safeStorage || typeof safeStorage.isEncryptionAvailable !== 'function' || !safeStorage.isEncryptionAvailable()) {
      return { masterKey: null, secretBackend: null };
    }
    const keyFile = path.join(app.getPath('userData'), 'secret-key.bin');
    if (fs.existsSync(keyFile)) {
      const cipherBuf = fs.readFileSync(keyFile);
      const decrypted = safeStorage.decryptString(cipherBuf).trim();
      if (decrypted) {
        return { masterKey: decrypted, secretBackend: 'electron_safe_storage' };
      }
    }
    const freshKey = crypto.randomBytes(32).toString('hex');
    const encrypted = safeStorage.encryptString(freshKey);
    fs.mkdirSync(path.dirname(keyFile), { recursive: true });
    fs.writeFileSync(keyFile, encrypted, { mode: 0o600 });
    return { masterKey: freshKey, secretBackend: 'electron_safe_storage' };
  } catch (err) {
    desktopLogger.warn('[security] Electron safeStorage unavailable; falling back to backend keystore:', err?.message || err);
    console.warn('[security] Electron safeStorage unavailable; falling back to backend keystore:', err?.message || err);
    return { masterKey: null, secretBackend: null };
  }
}

function assertTrustedIpcSender(event) {
  const frameUrl = event?.senderFrame?.url || '';
  if (!frameUrl || !isAllowedUiUrl(frameUrl, trustedOrigins)) {
    throw new Error('Blocked IPC request from untrusted origin');
  }
  if (mainWindow && !mainWindow.isDestroyed() && event.sender !== mainWindow.webContents) {
    throw new Error('Blocked IPC request from unexpected WebContents');
  }
}

function configureSessionSecurity(sess, allowedOrigins) {
  trustedOrigins = Array.from(new Set(allowedOrigins));
  if (sessionSecurityConfigured) {
    return;
  }
  sessionSecurityConfigured = true;

  // Deny all browser permission requests (camera, mic, geolocation, clipboard, notifications, etc.)
  sess.setPermissionRequestHandler((_wc, _permission, callback) => {
    callback(false);
  });
  if (typeof sess.setPermissionCheckHandler === 'function') {
    sess.setPermissionCheckHandler(() => false);
  }

  // Inject local per-session API auth token ONLY to trusted local backend/UI origins
  sess.webRequest.onBeforeSendHeaders((details, callback) => {
    const headers = { ...details.requestHeaders };
    if (backendHandle && backendHandle.apiToken) {
      try {
        const parsed = new URL(details.url);
        const backendOrigin = new URL(backendHandle.url).origin;
        const isBackendTarget = parsed.origin === backendOrigin;
        const isDevProxyTarget =
          !app.isPackaged &&
          trustedOrigins.includes(parsed.origin) &&
          (parsed.pathname.startsWith('/api/') ||
            parsed.pathname === '/openapi.json' ||
            parsed.pathname === '/docs');
        if (isBackendTarget || isDevProxyTarget) {
          headers['X-BillApp-Token'] = backendHandle.apiToken;
        }
      } catch {
        // ignore invalid URL
      }
    }
    callback({ requestHeaders: headers });
  });

  // Inject strict Content-Security-Policy for local UI documents
  sess.webRequest.onHeadersReceived((details, callback) => {
    const responseHeaders = { ...(details.responseHeaders || {}) };
    try {
      const parsed = new URL(details.url);
      if (trustedOrigins.includes(parsed.origin) && details.resourceType === 'mainFrame') {
        const backendOrigin = backendHandle ? new URL(backendHandle.url).origin : parsed.origin;
        const connectSources = ["'self'", backendOrigin];
        if (!app.isPackaged) {
          connectSources.push('ws://127.0.0.1:*', 'ws://localhost:*');
        }
        const csp = [
          "default-src 'self'",
          "script-src 'self'",
          "style-src 'self' 'unsafe-inline'",
          "img-src 'self' data: blob:",
          "font-src 'self' data:",
          `connect-src ${Array.from(new Set(connectSources)).join(' ')}`,
          "object-src 'none'",
          "base-uri 'self'",
          "frame-ancestors 'none'",
          "form-action 'self'",
        ].join('; ');
        responseHeaders['Content-Security-Policy'] = [csp];
      }
    } catch {
      // ignore invalid URL
    }
    callback({ responseHeaders });
  });
}

function getUiEntryUrl() {
  // Never trust BILLAPP_DEV_URL in packaged production builds (S-04)
  if (!app.isPackaged) {
    const devUrl = (process.env.BILLAPP_DEV_URL || '').trim();
    if (devUrl) {
      return devUrl;
    }
  }
  if (!backendHandle) {
    throw new Error('Backend server is not running.');
  }
  return backendHandle.url;
}

function buildAppMenu() {
  if (process.platform === 'darwin') {
    const template = [
      {
        label: app.name,
        submenu: [
          { role: 'about', label: '关于 账单助手' },
          { type: 'separator' },
          { role: 'services' },
          { type: 'separator' },
          { role: 'hide', label: '隐藏 账单助手' },
          { role: 'hideOthers', label: '隐藏其他' },
          { role: 'unhide', label: '全部显示' },
          { type: 'separator' },
          { role: 'quit', label: '退出 账单助手' },
        ],
      },
      {
        label: '编辑',
        submenu: [
          { role: 'undo', label: '撤销' },
          { role: 'redo', label: '重做' },
          { type: 'separator' },
          { role: 'cut', label: '剪切' },
          { role: 'copy', label: '复制' },
          { role: 'paste', label: '粘贴' },
          { role: 'selectAll', label: '全选' },
        ],
      },
      {
        label: '视图',
        submenu: [
          { role: 'reload', label: '刷新' },
          { role: 'forceReload', label: '强制刷新' },
          { role: 'toggleDevTools', label: '开发者工具' },
          { type: 'separator' },
          { role: 'resetZoom', label: '实际大小' },
          { role: 'zoomIn', label: '放大' },
          { role: 'zoomOut', label: '缩小' },
          { type: 'separator' },
          { role: 'togglefullscreen', label: '全屏' },
        ],
      },
      {
        label: '窗口',
        submenu: [
          { role: 'minimize', label: '最小化' },
          { role: 'zoom', label: '缩放' },
          { role: 'close', label: '关闭窗口' },
        ],
      },
    ];
    return Menu.buildFromTemplate(template);
  }
  return null;
}

function isAllowedUiUrl(targetUrl, allowedOrigins) {
  try {
    const parsed = new URL(targetUrl);
    return allowedOrigins.includes(parsed.origin);
  } catch {
    return false;
  }
}

function refreshTrustedOrigins() {
  const uiUrl = getUiEntryUrl();
  const allowedOrigins = [new URL(uiUrl).origin];
  if (backendHandle) {
    allowedOrigins.push(new URL(backendHandle.url).origin);
  }
  configureSessionSecurity(session.defaultSession, allowedOrigins);
  return uiUrl;
}

function createMainWindow() {
  const uiUrl = refreshTrustedOrigins();

  const win = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 1024,
    minHeight: 680,
    title: WINDOW_TITLE,
    backgroundColor: '#F7F8FA',
    icon: resolveIconPath(),
    autoHideMenuBar: process.platform !== 'darwin',
    show: !SMOKE_TEST,
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webviewTag: false,
    },
  });

  if (process.platform !== 'darwin') {
    win.setMenuBarVisibility(false);
  }

  win.webContents.setWindowOpenHandler(({ url }) => {
    try {
      const parsed = new URL(url);
      // Convert API export window.open calls cleanly into native downloads
      if (
        trustedOrigins.includes(parsed.origin) &&
        parsed.pathname.startsWith('/api/export')
      ) {
        win.webContents.downloadURL(url);
        return { action: 'deny' };
      }
      if (parsed.protocol === 'http:' || parsed.protocol === 'https:') {
        shell.openExternal(url);
      }
    } catch {
      // ignore invalid URL
    }
    return { action: 'deny' };
  });

  win.webContents.on('will-navigate', (event, navigationUrl) => {
    if (!isAllowedUiUrl(navigationUrl, trustedOrigins)) {
      event.preventDefault();
    }
  });

  win.webContents.on('did-fail-load', (_event, errorCode, errorDescription, validatedURL) => {
    desktopLogger.error(
      `[renderer] did-fail-load (${errorCode}): ${errorDescription} url=${validatedURL || ''}`
    );
  });

  win.webContents.on('render-process-gone', (_event, details) => {
    desktopLogger.error(
      `[renderer] render-process-gone reason=${details?.reason || 'unknown'} exitCode=${details?.exitCode ?? ''}`
    );
  });

  win.webContents.on('console-message', (_event, level, message, line, sourceId) => {
    // Capture renderer console warnings (2) and errors (3)
    if (level >= 2) {
      const tag = level >= 3 ? 'error' : 'warn';
      desktopLogger[tag](`[renderer:console] ${message} (${sourceId || ''}:${line || 0})`);
    }
  });

  if (!downloadListenerRegistered) {
    downloadListenerRegistered = true;
    win.webContents.session.on('will-download', (_event, item) => {
      const filename = sanitizeDownloadFilename(item.getFilename(), 'billapp-export.xlsx');
      if (!app.isPackaged) {
        const testDir = (process.env.BILLAPP_TEST_DOWNLOAD_DIR || '').trim();
        if (testDir) {
          fs.mkdirSync(testDir, { recursive: true });
          item.setSavePath(path.join(testDir, filename));
          return;
        }
      }
      const downloadsDir = app.getPath('downloads');
      item.setSaveDialogOptions({
        title: '保存导出文件',
        defaultPath: path.join(downloadsDir, filename),
      });
    });
  }

  win.on('closed', () => {
    if (mainWindow === win) {
      mainWindow = null;
    }
  });

  if (!app.isPackaged && process.env.BILLAPP_OPEN_DEVTOOLS === '1' && !SMOKE_TEST) {
    win.webContents.openDevTools({ mode: 'detach' });
  }

  mainWindow = win;
  return { win, uiUrl };
}

function getAppUpdaterController() {
  if (!appUpdaterController) {
    appUpdaterController = createAppUpdater({
      app,
      openUrl: (url) => shell.openExternal(url),
      beforeInstall: async () => {
        isQuitting = true;
        await shutdownBackend();
      },
      onInstallFailed: async () => {
        isQuitting = false;
        try {
          await ensureBackendStarted();
          const nextUiUrl = refreshTrustedOrigins();
          if (mainWindow && !mainWindow.isDestroyed()) {
            await mainWindow.loadURL(nextUiUrl);
          }
        } catch (restartErr) {
          desktopLogger.error('[updater] Failed to restart backend after install error:', restartErr);
          console.error('[updater] Failed to restart backend after install error:', restartErr);
        }
      },
      fetchImpl: (url, opts) => net.fetch(url, opts),
      logger: desktopLogger,
      onStateChange: (state) => {
        if (mainWindow && !mainWindow.isDestroyed()) {
          mainWindow.webContents.send('bill:updater-state', state);
        }
      },
    });
    appUpdaterController.init();
  }
  return appUpdaterController;
}

function registerIpcHandlers() {
  const updater = getAppUpdaterController();

  ipcMain.handle('bill:get-desktop-info', (event) => {
    assertTrustedIpcSender(event);
    const logInfo = desktopLogger.getInfo();
    return {
      platform: process.platform,
      isPackaged: app.isPackaged,
      appVersion: app.getVersion(),
      backendUrl: backendHandle ? backendHandle.url : undefined,
      installMode: updater.getState().installMode,
      desktopLogPath: logInfo.logPath,
      desktopLogSizeBytes: logInfo.totalSizeBytes,
      desktopLogFileCount: logInfo.files.length,
    };
  });

  ipcMain.handle('bill:updater-get-state', (event) => {
    assertTrustedIpcSender(event);
    return updater.getState();
  });
  ipcMain.handle('bill:updater-check', (event) => {
    assertTrustedIpcSender(event);
    return updater.checkForUpdates({ silent: false });
  });
  ipcMain.handle('bill:updater-download', (event) => {
    assertTrustedIpcSender(event);
    return updater.downloadUpdate();
  });
  ipcMain.handle('bill:updater-quit-and-install', (event) => {
    assertTrustedIpcSender(event);
    return updater.quitAndInstall();
  });
  ipcMain.handle('bill:updater-open-release', (event) => {
    assertTrustedIpcSender(event);
    return updater.openReleasePage();
  });

  ipcMain.handle('bill:save-database-backup', async (event, payload = {}) => {
    assertTrustedIpcSender(event);
    if (!backendHandle) {
      return { ok: false, cancelled: false, error: '后台服务尚未就绪' };
    }

    const stamp = new Date().toISOString().replace(/[:T]/g, '-').slice(0, 19);
    const defaultFallback = `billapp-backup-${stamp}.db`;
    let filename = sanitizeDownloadFilename(payload?.defaultFilename, defaultFallback);
    if (!filename.toLowerCase().endsWith('.db')) {
      filename = `${filename}.db`;
    }

    let defaultDir;
    try {
      defaultDir = app.getPath('desktop');
    } catch {
      defaultDir = app.getPath('downloads');
    }

    const dialogResult = await dialog.showSaveDialog(mainWindow || undefined, {
      title: '导出数据库备份',
      defaultPath: path.join(defaultDir, filename),
      filters: [
        { name: 'SQLite 数据库备份 (.db)', extensions: ['db', 'sqlite'] },
        { name: '所有文件', extensions: ['*'] },
      ],
    });

    if (dialogResult.canceled || !dialogResult.filePath) {
      return { ok: false, cancelled: true };
    }

    let targetPath = dialogResult.filePath.trim();
    if (!path.extname(targetPath)) {
      targetPath = `${targetPath}.db`;
    }

    try {
      const headers = backendHandle.apiToken
        ? { 'X-BillApp-Token': backendHandle.apiToken }
        : {};
      const { buffer } = await httpPostBuffer(`${backendHandle.url}/api/backup`, 30000, headers);
      await fs.promises.mkdir(path.dirname(targetPath), { recursive: true });
      await fs.promises.writeFile(targetPath, buffer);
      return {
        ok: true,
        cancelled: false,
        path: targetPath,
        filename: path.basename(targetPath),
        size_bytes: buffer.length,
      };
    } catch (err) {
      return {
        ok: false,
        cancelled: false,
        error: err?.message || '写入数据库备份失败',
      };
    }
  });

  ipcMain.handle('bill:export-logs', async (event, payload = {}) => {
    assertTrustedIpcSender(event);
    if (!backendHandle) {
      return { ok: false, cancelled: false, error: '后台服务尚未就绪' };
    }

    const format = payload?.format === 'log' ? 'log' : 'zip';
    const ext = format === 'log' ? '.log' : '.zip';
    const stamp = new Date().toISOString().replace(/[:T]/g, '-').slice(0, 19);
    const defaultFallback = `billapp-logs-${stamp}${ext}`;
    let filename = sanitizeDownloadFilename(payload?.defaultFilename, defaultFallback);
    if (!filename.toLowerCase().endsWith(ext)) {
      filename = `${filename}${ext}`;
    }

    let defaultDir;
    try {
      defaultDir = app.getPath('desktop');
    } catch {
      defaultDir = app.getPath('downloads');
    }

    const dialogResult = await dialog.showSaveDialog(mainWindow || undefined, {
      title: format === 'zip' ? '导出诊断日志包' : '导出运行日志',
      defaultPath: path.join(defaultDir, filename),
      filters:
        format === 'zip'
          ? [
              { name: '诊断日志压缩包 (.zip)', extensions: ['zip'] },
              { name: '所有文件', extensions: ['*'] },
            ]
          : [
              { name: '纯文本日志文件 (.log)', extensions: ['log', 'txt'] },
              { name: '所有文件', extensions: ['*'] },
            ],
    });

    if (dialogResult.canceled || !dialogResult.filePath) {
      return { ok: false, cancelled: true };
    }

    let targetPath = dialogResult.filePath.trim();
    if (!path.extname(targetPath)) {
      targetPath = `${targetPath}${ext}`;
    }

    try {
      desktopLogger.info(`Exporting diagnostic logs (format=${format}) via desktop Save-As dialog`);
      const headers = backendHandle.apiToken
        ? { 'X-BillApp-Token': backendHandle.apiToken }
        : {};
      const requestBody = {
        format,
        include_diagnostics: payload?.includeDiagnostics !== false,
        redact_pii: payload?.redactPii !== false,
        frontend_logs: Array.isArray(payload?.frontendLogs) ? payload.frontendLogs.slice(-500) : [],
        desktop_logs: desktopLogger.collectLogs(),
        desktop_meta: {
          platform: process.platform,
          arch: process.arch,
          appVersion: app.getVersion(),
          isPackaged: app.isPackaged,
          installMode: updater.getState().installMode,
          versions: {
            electron: process.versions.electron,
            chrome: process.versions.chrome,
            node: process.versions.node,
          },
        },
      };
      const { buffer } = await httpPostJsonForBuffer(
        `${backendHandle.url}/api/logs/export`,
        requestBody,
        30000,
        headers
      );
      await fs.promises.mkdir(path.dirname(targetPath), { recursive: true });
      await fs.promises.writeFile(targetPath, buffer);
      return {
        ok: true,
        cancelled: false,
        path: targetPath,
        filename: path.basename(targetPath),
        size_bytes: buffer.length,
      };
    } catch (err) {
      desktopLogger.error('Failed to export diagnostic logs:', err);
      return {
        ok: false,
        cancelled: false,
        error: err?.message || '导出日志文件失败',
      };
    }
  });
}

async function ensureBackendStarted() {
  if (backendHandle && backendHandle.isRunning()) {
    return backendHandle;
  }
  if (startInFlight) {
    return startInFlight;
  }

  startInFlight = (async () => {
    // Never trust external BILLAPP_BACKEND_URL override in packaged builds (S-04)
    if (!app.isPackaged) {
      const externalUrl = (process.env.BILLAPP_BACKEND_URL || '').trim();
      if (externalUrl) {
        const parsed = new URL(externalUrl);
        backendHandle = {
          host: parsed.hostname,
          port: Number(parsed.port) || 8000,
          url: externalUrl.replace(/\/+$/, ''),
          apiToken: (process.env.BILLAPP_API_TOKEN || '').trim() || null,
          dbPath: parseDbCliArg() || process.env.BILLAPP_DB_PATH || null,
          frontendDist: resolveFrontendDistDir(),
          stop: async () => {},
          isRunning: () => true,
        };
        return backendHandle;
      }
    }

    const dbPath = parseDbCliArg() || (process.env.BILLAPP_DB_PATH || '').trim() || null;
    const requestedPort = !app.isPackaged ? Number(process.env.BILLAPP_BACKEND_PORT || 0) || 0 : 0;
    const { masterKey, secretBackend } = getOrCreateSafeStorageMasterKey();

    backendHandle = await startBackend({
      isPackaged: app.isPackaged,
      resourcesPath: process.resourcesPath,
      repoRoot: getRepoRoot(),
      requestedPort,
      dbPath,
      frontendDir: resolveFrontendDistDir(),
      masterKey,
      secretBackend,
      onLog: (stream, text) => desktopLogger.logBackend(stream, text),
    });
    desktopLogger.info(`Backend sidecar started on ${backendHandle.url}`);
    return backendHandle;
  })().finally(() => {
    startInFlight = null;
  });

  return startInFlight;
}

async function shutdownBackend() {
  if (!backendHandle) {
    return;
  }
  if (!stopInFlight) {
    stopInFlight = backendHandle.stop().catch(() => {}).finally(() => {
      backendHandle = null;
      stopInFlight = null;
    });
  }
  await stopInFlight;
}

async function loadAndMaybeSmokeTest({ win, uiUrl }) {
  if (!SMOKE_TEST) {
    await win.loadURL(uiUrl);
    return;
  }

  const loadPromise = new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error('Window load timed out in smoke test')), 15000);
    win.webContents.once('did-finish-load', () => {
      clearTimeout(timeout);
      resolve();
    });
    win.webContents.once('did-fail-load', (_e, code, desc) => {
      clearTimeout(timeout);
      reject(new Error(`did-fail-load (${code}): ${desc}`));
    });
  });
  await Promise.all([loadPromise, win.loadURL(uiUrl)]);

  // Give Vue mount a brief tick to render #app content
  const probe = await win.webContents.executeJavaScript(`
    (async () => {
      const deadline = Date.now() + 8000;
      while (Date.now() < deadline) {
        const root = document.querySelector('#app');
        if (root && root.innerHTML.trim().length > 0 && window.billDesktop) {
          break;
        }
        await new Promise((r) => setTimeout(r, 80));
      }
      const health = await fetch('/api/health').then((r) => r.json());
      const meta = await fetch('/api/meta').then((r) => r.json());
      return {
        title: document.title,
        hasAppHtml: (document.querySelector('#app')?.innerHTML?.length || 0) > 0,
        billDesktop: window.billDesktop ? {
          isElectron: window.billDesktop.isElectron,
          platform: window.billDesktop.platform,
          hasSaveBackup: typeof window.billDesktop.saveDatabaseBackup === 'function',
          hasExportLogs: typeof window.billDesktop.exportLogs === 'function',
          hasUpdater: typeof window.billDesktop.updater?.checkForUpdates === 'function',
          info: await window.billDesktop.getDesktopInfo(),
          updaterState: window.billDesktop.updater ? await window.billDesktop.updater.getState() : null,
        } : null,
        health,
        metaOk: Boolean(meta && meta.version),
      };
    })()
  `);

  if (!probe?.hasAppHtml || !probe?.billDesktop?.isElectron || !probe?.billDesktop?.hasExportLogs || !probe?.billDesktop?.hasUpdater || probe?.health?.status !== 'ok' || !probe?.metaOk) {
    throw new Error(`Smoke test probe failed: ${JSON.stringify(probe)}`);
  }

  console.log(`BILLAPP_ELECTRON_SMOKETEST_OK ${JSON.stringify(probe)}`);
}

if (hasSingleInstanceLock) {
  app.on('web-contents-created', (_event, contents) => {
    contents.on('will-attach-webview', (event) => {
      event.preventDefault();
    });
  });

  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) {
        mainWindow.restore();
      }
      mainWindow.show();
      mainWindow.focus();
    }
  });

  app.on('activate', async () => {
    if (!app.isReady()) return;
    try {
      if (BrowserWindow.getAllWindows().length === 0) {
        await ensureBackendStarted();
        const { win, uiUrl } = createMainWindow();
        await win.loadURL(uiUrl);
      } else if (mainWindow) {
        mainWindow.show();
      }
    } catch (err) {
      console.error('Failed to restore BillApp window on activate:', err);
      if (!SMOKE_TEST) {
        dialog.showErrorBox('账单助手恢复窗口失败', err?.message || String(err));
      }
    }
  });

  app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') {
      app.quit();
    }
  });

  app.on('before-quit', (event) => {
    if (isQuitting) {
      return;
    }
    event.preventDefault();
    isQuitting = true;
    shutdownBackend().finally(() => {
      app.quit();
    });
  });

  app.whenReady()
    .then(async () => {
      desktopLogger.info(
        `=== BillApp Electron starting (version=${app.getVersion()}, packaged=${app.isPackaged}, platform=${process.platform}/${process.arch}) ===`
      );
      const appMenu = buildAppMenu();
      Menu.setApplicationMenu(appMenu);
      registerIpcHandlers();
      await ensureBackendStarted();
      const created = createMainWindow();
      await loadAndMaybeSmokeTest(created);
      if (SMOKE_TEST) {
        app.quit();
      } else {
        const startupTimer = setTimeout(() => {
          getAppUpdaterController().checkForUpdates({ silent: true }).catch(() => {});
        }, 5000);
        startupTimer.unref();
      }
    })
    .catch(async (err) => {
      desktopLogger.error('Failed to start BillApp Electron shell:', err);
      console.error('Failed to start BillApp Electron shell:', err);
      if (!SMOKE_TEST) {
        dialog.showErrorBox('账单助手启动失败', err?.message || String(err));
      }
      await shutdownBackend();
      app.exit(1);
    });
}
