'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');

const {
  compareSemver,
  createAppUpdater,
  detectInstallMode,
  formatUpdaterError,
  normalizeReleaseNotes,
  parseSemver,
} = require('../src/updater.cjs');

class MockAutoUpdater extends EventEmitter {
  constructor() {
    super();
    this.autoDownload = true;
    this.autoInstallOnAppQuit = true;
    this.allowPrerelease = true;
    this.fullChangelog = true;
    this.quitAndInstallCalls = [];
    this.checkBehavior = async () => ({ updateInfo: { version: '1.2.0' } });
    this.downloadBehavior = async () => ['/tmp/BillApp-Setup-1.3.0.exe'];
  }

  async checkForUpdates() {
    return this.checkBehavior();
  }

  async downloadUpdate() {
    return this.downloadBehavior();
  }

  quitAndInstall(isSilent, isForceRunAfter) {
    this.quitAndInstallCalls.push({ isSilent, isForceRunAfter });
  }
}

test('parseSemver and compareSemver handle stable and prerelease versions', () => {
  assert.deepEqual(parseSemver('v1.2.0'), { major: 1, minor: 2, patch: 0, prerelease: [] });
  assert.equal(compareSemver('1.3.0', '1.2.0'), 1);
  assert.equal(compareSemver('v1.2.0', '1.2.0'), 0);
  assert.equal(compareSemver('1.1.9', '1.2.0'), -1);
  assert.equal(compareSemver('1.2.0', '1.2.0-beta.1'), 1);
  assert.equal(compareSemver('1.2.0-beta.2', '1.2.0-beta.1'), 1);
});

test('normalizeReleaseNotes sanitizes HTML release notes safely to readable text', () => {
  const html = '<p>本次更新内容：</p><ul><li>新增自动更新</li><li>修复 &amp; 优化性能</li></ul>';
  assert.equal(normalizeReleaseNotes(html), '本次更新内容：\n- 新增自动更新\n- 修复 & 优化性能');

  const multi = [{ version: '1.3.0', note: '<p>Bug fixes</p>' }];
  assert.equal(normalizeReleaseNotes(multi), 'v1.3.0\nBug fixes');
});

test('detectInstallMode distinguishes NSIS, Portable, Mac, AppImage, Deb, and Dev', () => {
  assert.equal(detectInstallMode({ platform: 'win32', isPackaged: false }), 'dev');
  assert.equal(detectInstallMode({ platform: 'win32', isPackaged: true, env: {} }), 'nsis');
  assert.equal(
    detectInstallMode({ platform: 'win32', isPackaged: true, env: { PORTABLE_EXECUTABLE_DIR: 'D:\\Apps' } }),
    'portable'
  );
  assert.equal(detectInstallMode({ platform: 'darwin', isPackaged: true, env: {} }), 'mac');
  assert.equal(
    detectInstallMode({ platform: 'linux', isPackaged: true, env: { APPIMAGE: '/home/u/BillApp.AppImage' } }),
    'appimage'
  );
});

test('formatUpdaterError translates network, 404, and unsigned macOS Squirrel errors', () => {
  const macErr = formatUpdaterError(new Error('Code signature at URL file:///tmp/a.zip did not pass validation'), 'darwin');
  assert.equal(macErr.requiresManualDownload, true);
  assert.match(macErr.message, /macOS/);

  const notFound = formatUpdaterError(new Error('ERR_UPDATER_LATEST_VERSION_NOT_FOUND: 404'));
  assert.equal(notFound.requiresManualDownload, false);
  assert.match(notFound.message, /GitHub Releases/);

  const netErr = formatUpdaterError(new Error('net::ERR_CONNECTION_TIMED_OUT'));
  assert.match(netErr.message, /超时|网络/);
});

test('NSIS updater runs check -> download -> beforeInstall -> quitAndInstall cleanly', async () => {
  const mockAuto = new MockAutoUpdater();
  const events = [];
  let backendStopped = false;

  mockAuto.checkBehavior = async () => {
    mockAuto.emit('checking-for-update');
    mockAuto.emit('update-available', {
      version: '1.3.0',
      releaseDate: '2026-12-01T00:00:00Z',
      releaseNotes: '<p>新增自动同步功能</p>',
    });
    return {};
  };

  mockAuto.downloadBehavior = async () => {
    mockAuto.emit('download-progress', { percent: 45.5, bytesPerSecond: 1024000, transferred: 4550, total: 10000 });
    mockAuto.emit('update-downloaded', { version: '1.3.0' });
    return ['/tmp/BillApp-Setup-1.3.0.exe'];
  };

  const controller = createAppUpdater({
    app: { isPackaged: true, getVersion: () => '1.2.0' },
    autoUpdater: mockAuto,
    platform: 'win32',
    env: {},
    beforeInstall: async () => {
      backendStopped = true;
    },
    onStateChange: (s) => events.push(s.status),
  });

  const checked = await controller.checkForUpdates({ silent: false });
  assert.equal(mockAuto.autoDownload, false);
  assert.equal(mockAuto.autoInstallOnAppQuit, false);
  assert.equal(checked.status, 'available');
  assert.equal(checked.latestVersion, '1.3.0');
  assert.equal(checked.releaseNotes, '新增自动同步功能');
  assert.equal(checked.manualDownloadRequired, false);

  const downloaded = await controller.downloadUpdate();
  assert.equal(downloaded.status, 'downloaded');
  assert.equal(downloaded.downloadProgress.percent, 100);

  const installRes = await controller.quitAndInstall();
  assert.equal(installRes.ok, true);
  assert.equal(backendStopped, true);
  assert.deepEqual(mockAuto.quitAndInstallCalls, [{ isSilent: false, isForceRunAfter: true }]);
  assert.ok(events.includes('checking'));
  assert.ok(events.includes('available'));
  assert.ok(events.includes('downloading'));
  assert.ok(events.includes('downloaded'));
});

test('silent startup check swallows network errors without surfacing error banner', async () => {
  const mockAuto = new MockAutoUpdater();
  mockAuto.checkBehavior = async () => {
    mockAuto.emit('checking-for-update');
    const err = new Error('net::ERR_INTERNET_DISCONNECTED');
    mockAuto.emit('error', err);
    throw err;
  };

  const controller = createAppUpdater({
    app: { isPackaged: true, getVersion: () => '1.2.0' },
    autoUpdater: mockAuto,
    platform: 'win32',
    env: {},
  });

  const state = await controller.checkForUpdates({ silent: true });
  assert.equal(state.status, 'idle');
  assert.equal(state.error, null);
});

test('unsigned macOS build detects update and marks manualDownloadRequired cleanly', async () => {
  const mockAuto = new MockAutoUpdater();
  mockAuto.checkBehavior = async () => {
    mockAuto.emit('checking-for-update');
    mockAuto.emit('update-available', { version: '1.3.0' });
    return {};
  };

  let openedUrl = '';
  const controller = createAppUpdater({
    app: { isPackaged: true, getVersion: () => '1.2.0' },
    autoUpdater: mockAuto,
    platform: 'darwin',
    checkMacSignature: () => false,
    openUrl: async (url) => {
      openedUrl = url;
    },
  });

  const state = await controller.checkForUpdates({ silent: false });
  assert.equal(state.installMode, 'mac');
  assert.equal(state.autoInstallSupported, false);
  assert.equal(state.manualDownloadRequired, true);
  assert.equal(state.status, 'available');
  assert.equal(state.releaseUrl, 'https://github.com/sjzsd147/BillApp/releases/tag/v1.3.0');

  await controller.openReleasePage();
  assert.equal(openedUrl, 'https://github.com/sjzsd147/BillApp/releases/tag/v1.3.0');
});

test('Windows portable and dev modes query GitHub releases safely and require manual download', async () => {
  const controller = createAppUpdater({
    app: { isPackaged: true, getVersion: () => '1.2.0' },
    platform: 'win32',
    env: { PORTABLE_EXECUTABLE_DIR: 'D:\\Portable' },
    fetchImpl: async () => ({
      ok: true,
      url: 'https://github.com/sjzsd147/BillApp/releases/tag/v1.3.0',
      text: async () =>
        JSON.stringify({
          tag_name: 'v1.3.0',
          published_at: '2026-12-01T08:00:00Z',
          body: 'Release v1.3.0',
        }),
    }),
  });

  const state = await controller.checkForUpdates({ silent: false });
  assert.equal(state.installMode, 'portable');
  assert.equal(state.status, 'available');
  assert.equal(state.latestVersion, '1.3.0');
  assert.equal(state.manualDownloadRequired, true);
  assert.equal(state.releaseNotes, 'Release v1.3.0');
});
