'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

const DEFAULT_OWNER = 'sjzsd147';
const DEFAULT_REPO = 'BillApp';

function stripLeadingV(version) {
  return String(version || '').trim().replace(/^v/i, '');
}

function parseSemver(version) {
  const cleaned = stripLeadingV(version);
  const match = /^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$/.exec(cleaned);
  if (!match) {
    return null;
  }
  return {
    major: Number(match[1]),
    minor: Number(match[2]),
    patch: Number(match[3]),
    prerelease: match[4] ? match[4].split('.') : [],
  };
}

function compareSemver(a, b) {
  const pa = parseSemver(a);
  const pb = parseSemver(b);
  if (!pa || !pb) {
    return 0;
  }
  if (pa.major !== pb.major) return pa.major > pb.major ? 1 : -1;
  if (pa.minor !== pb.minor) return pa.minor > pb.minor ? 1 : -1;
  if (pa.patch !== pb.patch) return pa.patch > pb.patch ? 1 : -1;

  // Stable version > prerelease version
  if (pa.prerelease.length === 0 && pb.prerelease.length > 0) return 1;
  if (pa.prerelease.length > 0 && pb.prerelease.length === 0) return -1;

  const len = Math.max(pa.prerelease.length, pb.prerelease.length);
  for (let i = 0; i < len; i += 1) {
    const segA = pa.prerelease[i];
    const segB = pb.prerelease[i];
    if (segA === undefined) return -1;
    if (segB === undefined) return 1;
    const numA = /^\d+$/.test(segA) ? Number(segA) : null;
    const numB = /^\d+$/.test(segB) ? Number(segB) : null;
    if (numA !== null && numB !== null) {
      if (numA !== numB) return numA > numB ? 1 : -1;
    } else if (numA !== null) {
      return -1;
    } else if (numB !== null) {
      return 1;
    } else if (segA !== segB) {
      return segA > segB ? 1 : -1;
    }
  }
  return 0;
}

function decodeHtmlEntities(text) {
  return text
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&nbsp;/g, ' ');
}

function normalizeReleaseNotes(rawNotes) {
  if (!rawNotes) {
    return null;
  }
  let combined = '';
  if (Array.isArray(rawNotes)) {
    combined = rawNotes
      .map((entry) => {
        if (!entry) return '';
        const ver = entry.version ? `v${stripLeadingV(entry.version)}` : '';
        const note = typeof entry.note === 'string' ? entry.note : '';
        return ver ? `${ver}\n${note}` : note;
      })
      .filter(Boolean)
      .join('\n\n');
  } else if (typeof rawNotes === 'string') {
    combined = rawNotes;
  } else {
    return null;
  }

  const normalized = decodeHtmlEntities(
    combined
      .replace(/<br\s*\/?>/gi, '\n')
      .replace(/<\/(p|div|h[1-6]|tr)>/gi, '\n')
      .replace(/<li[^>]*>/gi, '- ')
      .replace(/<\/li>/gi, '\n')
      .replace(/<[^>]+>/g, '')
  )
    .replace(/\r\n/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();

  return normalized || null;
}

function detectInstallMode({
  platform = process.platform,
  isPackaged = false,
  env = process.env,
  resourcesPath = process.resourcesPath || '',
} = {}) {
  if (!isPackaged) {
    return 'dev';
  }
  if (platform === 'win32') {
    return env.PORTABLE_EXECUTABLE_DIR ? 'portable' : 'nsis';
  }
  if (platform === 'darwin') {
    return 'mac';
  }
  if (platform === 'linux') {
    if (env.APPIMAGE) {
      return 'appimage';
    }
    try {
      const pkgTypePath = path.join(resourcesPath, 'package-type');
      if (resourcesPath && fs.existsSync(pkgTypePath)) {
        const pkgType = fs.readFileSync(pkgTypePath, 'utf8').trim().toLowerCase();
        if (['deb', 'rpm', 'pacman'].includes(pkgType)) {
          return pkgType;
        }
      }
    } catch {
      // ignore missing/unreadable package-type
    }
    return 'linux';
  }
  return 'unknown';
}

function hasValidMacDeveloperSignature(execPath = process.execPath) {
  if (process.platform !== 'darwin') {
    return true;
  }
  try {
    const appBundleIdx = execPath.indexOf('.app/');
    const targetPath = appBundleIdx >= 0 ? execPath.slice(0, appBundleIdx + 4) : execPath;
    const output = execFileSync('codesign', ['--display', '--verbose=2', targetPath], {
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'pipe'],
      timeout: 3000,
    });
    return /Authority=/.test(output) && !/Signature=adhoc/i.test(output);
  } catch (err) {
    const stderr = String(err?.stderr || '');
    return /Authority=/.test(stderr) && !/Signature=adhoc/i.test(stderr);
  }
}

function formatUpdaterError(err, platform = process.platform) {
  const raw = String(err?.message || err || '').trim();
  const lower = raw.toLowerCase();

  if (
    platform === 'darwin' &&
    (lower.includes('code signature') ||
      lower.includes('did not pass validation') ||
      lower.includes('squirrel') ||
      lower.includes('not signed'))
  ) {
    return {
      message: '当前 macOS 版本未经过 Apple 签名认证，无法应用内覆盖安装，请前往 GitHub Releases 手动下载最新安装包。',
      requiresManualDownload: true,
    };
  }

  if (
    lower.includes('err_updater_latest_version_not_found') ||
    lower.includes('err_updater_channel_file_not_found') ||
    lower.includes('no published versions on github') ||
    lower.includes('404')
  ) {
    return {
      message: '未获取到 GitHub Releases 更新信息（可能仓库尚未公开发布对应平台安装包）。',
      requiresManualDownload: false,
    };
  }

  if (
    lower.includes('err_internet_disconnected') ||
    lower.includes('err_connection') ||
    lower.includes('enotfound') ||
    lower.includes('econnrefused') ||
    lower.includes('etimedout') ||
    lower.includes('timeout') ||
    lower.includes('net::')
  ) {
    return {
      message: '连接 GitHub 检查更新超时或网络不可达，请检查网络或代理设置后重试。',
      requiresManualDownload: false,
    };
  }

  if (lower.includes('err_updater_invalid_signature')) {
    return {
      message: '更新包签名校验未通过，为保障安全已中止安装，请前往 GitHub Releases 手动核验下载。',
      requiresManualDownload: true,
    };
  }

  if (lower.includes('sha512') || lower.includes('checksum')) {
    return {
      message: '安装包完整性校验失败，请重试下载或前往 GitHub Releases 手动获取。',
      requiresManualDownload: true,
    };
  }

  return {
    message: raw ? `更新检查或下载失败：${raw}` : '更新检查或下载失败，请稍后重试。',
    requiresManualDownload: false,
  };
}

async function fetchLatestReleaseFromGitHub({
  owner = DEFAULT_OWNER,
  repo = DEFAULT_REPO,
  fetchImpl,
} = {}) {
  const doFetch = fetchImpl || globalThis.fetch;
  if (typeof doFetch !== 'function') {
    throw new Error('HTTP fetch is unavailable in this environment.');
  }

  const latestUrl = `https://github.com/${owner}/${repo}/releases/latest`;
  const resp = await doFetch(latestUrl, {
    headers: {
      Accept: 'application/json',
      'User-Agent': 'BillApp-Desktop-Updater',
    },
  });

  if (!resp.ok) {
    const err = new Error(`HTTP ${resp.status} while querying GitHub Releases`);
    err.statusCode = resp.status;
    throw err;
  }

  const text = await resp.text();
  let tagName = '';
  let releaseUrl = latestUrl;
  let releaseDate = null;
  let releaseNotes = null;

  try {
    const json = JSON.parse(text);
    tagName = String(json.tag_name || '').trim();
    if (tagName) {
      releaseUrl = `https://github.com/${owner}/${repo}/releases/tag/${encodeURIComponent(tagName)}`;
    }
    releaseDate = json.published_at || json.created_at || null;
    releaseNotes = normalizeReleaseNotes(json.body || null);
  } catch {
    // Fallback if GitHub returns redirected HTML tag URL
    const urlMatch = /\/releases\/tag\/(v?[^/?#]+)/i.exec(resp.url || '');
    if (urlMatch) {
      tagName = urlMatch[1];
      releaseUrl = resp.url;
    }
  }

  const version = stripLeadingV(tagName);
  if (!parseSemver(version)) {
    throw new Error('ERR_UPDATER_LATEST_VERSION_NOT_FOUND');
  }

  return {
    tag: tagName,
    version,
    releaseUrl,
    releaseDate,
    releaseNotes,
  };
}

function createAppUpdater({
  app,
  autoUpdater,
  openUrl,
  beforeInstall,
  onStateChange,
  fetchImpl,
  owner = DEFAULT_OWNER,
  repo = DEFAULT_REPO,
  env = process.env,
  resourcesPath = process.resourcesPath || '',
  platform = process.platform,
  execPath = process.execPath,
  checkMacSignature = hasValidMacDeveloperSignature,
  logger = console,
} = {}) {
  const repositoryUrl = `https://github.com/${owner}/${repo}`;
  const releasesUrl = `${repositoryUrl}/releases`;
  const installMode = detectInstallMode({
    platform,
    isPackaged: Boolean(app?.isPackaged),
    env,
    resourcesPath,
  });
  const macSigned = platform === 'darwin' && app?.isPackaged ? Boolean(checkMacSignature(execPath)) : true;
  const autoInstallSupported =
    ['nsis', 'appimage', 'deb', 'rpm', 'pacman'].includes(installMode) ||
    (installMode === 'mac' && macSigned);

  const currentVersion = stripLeadingV(app?.getVersion?.() || '0.0.0');

  let state = {
    supported: true,
    autoInstallSupported,
    installMode,
    currentVersion,
    repositoryUrl,
    releasesUrl,
    status: 'idle',
    latestVersion: null,
    releaseDate: null,
    releaseNotes: null,
    releaseUrl: null,
    downloadProgress: null,
    manualDownloadRequired: !autoInstallSupported && installMode !== 'dev',
    checkedAt: null,
    error: null,
    silent: false,
  };

  let initialized = false;
  let checkPromise = null;
  let downloadPromise = null;
  let resolvedAutoUpdater = autoUpdater;

  function getAutoUpdater() {
    if (!resolvedAutoUpdater) {
      resolvedAutoUpdater = require('electron-updater').autoUpdater;
    }
    return resolvedAutoUpdater;
  }

  function getState() {
    return {
      ...state,
      downloadProgress: state.downloadProgress ? { ...state.downloadProgress } : null,
    };
  }

  function updateState(patch) {
    state = {
      ...state,
      ...patch,
    };
    if (typeof onStateChange === 'function') {
      onStateChange(getState());
    }
    return getState();
  }

  function buildTagUrl(version) {
    const clean = stripLeadingV(version);
    return clean ? `${repositoryUrl}/releases/tag/v${clean}` : releasesUrl;
  }

  function init() {
    if (initialized) {
      return;
    }
    initialized = true;

    if (!app?.isPackaged || installMode === 'portable' || installMode === 'linux' || installMode === 'unknown') {
      return;
    }

    const updater = getAutoUpdater();
    updater.autoDownload = false;
    updater.autoInstallOnAppQuit = false;
    updater.allowPrerelease = false;
    updater.fullChangelog = false;
    if (logger && typeof logger.info === 'function') {
      updater.logger = {
        info: (...args) => logger.info('[updater]', ...args),
        warn: (...args) => logger.warn('[updater]', ...args),
        error: (...args) => logger.error('[updater]', ...args),
        debug: () => {},
      };
    }

    updater.on('checking-for-update', () => {
      updateState({
        status: 'checking',
        error: null,
      });
    });

    updater.on('update-available', (info) => {
      const latestVersion = stripLeadingV(info?.version);
      updateState({
        status: 'available',
        latestVersion: latestVersion || null,
        releaseDate: info?.releaseDate || null,
        releaseNotes: normalizeReleaseNotes(info?.releaseNotes),
        releaseUrl: buildTagUrl(latestVersion),
        downloadProgress: null,
        manualDownloadRequired: !state.autoInstallSupported,
        checkedAt: new Date().toISOString(),
        error: null,
      });
    });

    updater.on('update-not-available', (info) => {
      const ver = stripLeadingV(info?.version || state.currentVersion);
      updateState({
        status: 'not-available',
        latestVersion: ver || state.currentVersion,
        releaseDate: info?.releaseDate || null,
        releaseNotes: null,
        releaseUrl: null,
        downloadProgress: null,
        manualDownloadRequired: false,
        checkedAt: new Date().toISOString(),
        error: null,
      });
    });

    updater.on('download-progress', (progress) => {
      updateState({
        status: 'downloading',
        downloadProgress: {
          percent: Math.min(100, Math.max(0, Number(progress?.percent || 0))),
          bytesPerSecond: Number(progress?.bytesPerSecond || 0),
          transferred: Number(progress?.transferred || 0),
          total: Number(progress?.total || 0),
        },
        error: null,
      });
    });

    updater.on('update-downloaded', (info) => {
      const ver = stripLeadingV(info?.version || state.latestVersion);
      const total = state.downloadProgress?.total || 0;
      updateState({
        status: 'downloaded',
        latestVersion: ver || state.latestVersion,
        releaseNotes: normalizeReleaseNotes(info?.releaseNotes) || state.releaseNotes,
        releaseUrl: buildTagUrl(ver || state.latestVersion),
        downloadProgress: {
          percent: 100,
          bytesPerSecond: 0,
          transferred: total,
          total,
        },
        manualDownloadRequired: false,
        error: null,
      });
    });

    updater.on('error', (err) => {
      if (state.silent) {
        logger?.warn?.('[updater] Silent check failed:', err?.message || err);
        updateState({
          status: state.latestVersion ? 'available' : 'idle',
          downloadProgress: null,
          error: null,
        });
        return;
      }
      const formatted = formatUpdaterError(err, platform);
      const keepAvailable = Boolean(state.latestVersion && formatted.requiresManualDownload);
      updateState({
        status: keepAvailable ? 'available' : 'error',
        downloadProgress: null,
        manualDownloadRequired: state.manualDownloadRequired || formatted.requiresManualDownload,
        error: formatted.message,
      });
    });
  }

  async function checkViaGitHubHttpFallback() {
    updateState({
      status: 'checking',
      error: null,
    });
    try {
      const latest = await fetchLatestReleaseFromGitHub({ owner, repo, fetchImpl });
      const cmp = compareSemver(latest.version, state.currentVersion);
      if (cmp > 0) {
        return updateState({
          status: 'available',
          latestVersion: latest.version,
          releaseDate: latest.releaseDate,
          releaseNotes: latest.releaseNotes,
          releaseUrl: latest.releaseUrl,
          downloadProgress: null,
          manualDownloadRequired: !state.autoInstallSupported,
          checkedAt: new Date().toISOString(),
          error: null,
        });
      }
      return updateState({
        status: 'not-available',
        latestVersion: latest.version,
        releaseDate: latest.releaseDate,
        releaseNotes: null,
        releaseUrl: null,
        downloadProgress: null,
        manualDownloadRequired: false,
        checkedAt: new Date().toISOString(),
        error: null,
      });
    } catch (err) {
      if (state.silent) {
        logger?.warn?.('[updater] Silent GitHub check failed:', err?.message || err);
        return updateState({
          status: 'idle',
          error: null,
        });
      }
      const formatted = formatUpdaterError(err, platform);
      return updateState({
        status: 'error',
        error: formatted.message,
        checkedAt: new Date().toISOString(),
      });
    }
  }

  async function checkForUpdates({ silent = false } = {}) {
    init();

    if (state.status === 'downloading' || state.status === 'downloaded') {
      return getState();
    }

    if (checkPromise) {
      if (!silent && state.silent) {
        updateState({ silent: false });
      }
      return checkPromise;
    }

    updateState({ silent: Boolean(silent), error: null });

    const useElectronUpdater =
      Boolean(app?.isPackaged) &&
      ['nsis', 'mac', 'appimage', 'deb', 'rpm', 'pacman'].includes(installMode);

    checkPromise = (async () => {
      if (!useElectronUpdater) {
        return checkViaGitHubHttpFallback();
      }
      try {
        const updater = getAutoUpdater();
        await updater.checkForUpdates();
        return getState();
      } catch (err) {
        if (state.silent) {
          return updateState({ status: 'idle', error: null });
        }
        // Fallback if updater wasn't active or threw before emitting error
        if (state.status === 'checking') {
          const formatted = formatUpdaterError(err, platform);
          return updateState({
            status: 'error',
            error: formatted.message,
            manualDownloadRequired: state.manualDownloadRequired || formatted.requiresManualDownload,
            checkedAt: new Date().toISOString(),
          });
        }
        return getState();
      }
    })().finally(() => {
      checkPromise = null;
    });

    return checkPromise;
  }

  async function downloadUpdate() {
    init();

    if (state.status === 'downloaded') {
      return getState();
    }
    if (downloadPromise) {
      return downloadPromise;
    }
    if (!app?.isPackaged) {
      return updateState({
        manualDownloadRequired: true,
        error: '当前为开发环境运行，请直接前往 GitHub Releases 下载正式安装包。',
      });
    }
    if (!state.autoInstallSupported || state.manualDownloadRequired) {
      return updateState({
        manualDownloadRequired: true,
        error:
          installMode === 'portable'
            ? 'Windows 便携版暂不支持应用内覆盖安装，请前往 GitHub Releases 下载新版替换。'
            : installMode === 'mac'
              ? '当前 macOS 版本未经过 Apple 签名认证，请前往 GitHub Releases 手动下载新安装包。'
              : '当前安装包类型暂不支持应用内自动更新，请前往 GitHub Releases 手动下载。',
      });
    }
    if (!state.latestVersion) {
      await checkForUpdates({ silent: false });
      if (state.status !== 'available') {
        return getState();
      }
    }

    updateState({
      silent: false,
      status: 'downloading',
      error: null,
      downloadProgress: {
        percent: 0,
        bytesPerSecond: 0,
        transferred: 0,
        total: 0,
      },
    });

    downloadPromise = (async () => {
      try {
        const updater = getAutoUpdater();
        await updater.downloadUpdate();
        return getState();
      } catch (err) {
        const formatted = formatUpdaterError(err, platform);
        const keepAvailable = Boolean(state.latestVersion && formatted.requiresManualDownload);
        return updateState({
          status: keepAvailable ? 'available' : 'error',
          downloadProgress: null,
          manualDownloadRequired: state.manualDownloadRequired || formatted.requiresManualDownload,
          error: formatted.message,
        });
      }
    })().finally(() => {
      downloadPromise = null;
    });

    return downloadPromise;
  }

  async function quitAndInstall() {
    if (state.status !== 'downloaded') {
      return {
        ok: false,
        error: '更新包尚未下载完成',
        state: getState(),
      };
    }

    // Ensure Python FastAPI sidecar checkpoints SQLite WAL & releases file locks before installer runs
    if (typeof beforeInstall === 'function') {
      await beforeInstall();
    }

    const updater = getAutoUpdater();
    updater.quitAndInstall(false, true);
    return {
      ok: true,
      state: getState(),
    };
  }

  function isValidReleaseUrl(targetUrl) {
    try {
      const raw = String(targetUrl || '').trim();
      const parsed = new URL(raw);
      if (parsed.protocol !== 'https:' || parsed.origin !== 'https://github.com') {
        return false;
      }
      if (parsed.username || parsed.password) {
        return false;
      }
      // Reject any traversal segments before URL normalization
      if (raw.includes('..') || raw.includes('\\')) {
        return false;
      }
      const normPath = path.posix.normalize(parsed.pathname);
      const expectedPrefix = `/${owner}/${repo}/releases`;
      return normPath === expectedPrefix || normPath.startsWith(`${expectedPrefix}/`);
    } catch {
      return false;
    }
  }

  async function openReleasePage(overrideUrl) {
    const targetUrl = overrideUrl || state.releaseUrl || releasesUrl;
    if (!isValidReleaseUrl(targetUrl)) {
      return { ok: false, error: '非法发布页地址' };
    }
    if (typeof openUrl === 'function') {
      await openUrl(targetUrl);
    }
    return { ok: true, url: targetUrl };
  }

  return {
    init,
    getState,
    checkForUpdates,
    downloadUpdate,
    quitAndInstall,
    openReleasePage,
  };
}

module.exports = {
  DEFAULT_OWNER,
  DEFAULT_REPO,
  compareSemver,
  createAppUpdater,
  detectInstallMode,
  fetchLatestReleaseFromGitHub,
  formatUpdaterError,
  hasValidMacDeveloperSignature,
  normalizeReleaseNotes,
  parseSemver,
};
