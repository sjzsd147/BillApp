import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  checkForUpdatesViaDesktop,
  downloadUpdateViaDesktop,
  getDesktopBridge,
  getDesktopUpdaterState,
  installUpdateViaDesktop,
  isElectronDesktop,
  openReleasePageViaDesktop,
  saveDatabaseBackupViaDesktop,
  subscribeDesktopUpdater,
} from '@/utils/desktop';

describe('desktop preload bridge helpers', () => {
  const originalBridge = window.billDesktop;

  afterEach(() => {
    window.billDesktop = originalBridge;
  });

  it('returns false and null gracefully in plain browser mode', async () => {
    window.billDesktop = undefined;
    expect(isElectronDesktop()).toBe(false);
    expect(getDesktopBridge()).toBeUndefined();
    expect(await saveDatabaseBackupViaDesktop('backup.db')).toBeNull();
    expect(await getDesktopUpdaterState()).toBeNull();
    expect(await checkForUpdatesViaDesktop()).toBeNull();
    expect(await downloadUpdateViaDesktop()).toBeNull();
    expect(await installUpdateViaDesktop()).toBeNull();
    const unsub = subscribeDesktopUpdater(() => {});
    expect(typeof unsub).toBe('function');
    unsub();
  });

  it('delegates database backup save dialog and updater actions when running in Electron', async () => {
    const saveDatabaseBackup = vi.fn().mockResolvedValue({
      ok: true,
      cancelled: false,
      path: '/tmp/backup.db',
      filename: 'backup.db',
      size_bytes: 4096,
    });
    const sampleState = {
      supported: true,
      autoInstallSupported: true,
      installMode: 'nsis',
      currentVersion: '1.2.0',
      repositoryUrl: 'https://github.com/sjzsd147/BillApp',
      releasesUrl: 'https://github.com/sjzsd147/BillApp/releases',
      status: 'available' as const,
      latestVersion: '1.3.0',
      releaseDate: null,
      releaseNotes: 'Fix bugs',
      releaseUrl: 'https://github.com/sjzsd147/BillApp/releases/tag/v1.3.0',
      downloadProgress: null,
      manualDownloadRequired: false,
      checkedAt: null,
      error: null,
    };
    const unsubscribe = vi.fn();
    const updater = {
      getState: vi.fn().mockResolvedValue(sampleState),
      checkForUpdates: vi.fn().mockResolvedValue(sampleState),
      downloadUpdate: vi.fn().mockResolvedValue({ ...sampleState, status: 'downloaded' as const }),
      quitAndInstall: vi.fn().mockResolvedValue({ ok: true }),
      openReleasePage: vi.fn().mockResolvedValue({ ok: true, url: sampleState.releaseUrl }),
      onStateChange: vi.fn().mockReturnValue(unsubscribe),
    };

    window.billDesktop = {
      isElectron: true,
      platform: 'win32',
      saveDatabaseBackup,
      updater,
    };

    expect(isElectronDesktop()).toBe(true);
    const res = await saveDatabaseBackupViaDesktop('backup.db');
    expect(saveDatabaseBackup).toHaveBeenCalledWith('backup.db');
    expect(res?.ok).toBe(true);

    expect(await getDesktopUpdaterState()).toEqual(sampleState);
    expect(await checkForUpdatesViaDesktop()).toEqual(sampleState);
    expect((await downloadUpdateViaDesktop())?.status).toBe('downloaded');
    expect((await installUpdateViaDesktop())?.ok).toBe(true);
    expect(await openReleasePageViaDesktop()).toBe(true);

    const listener = vi.fn();
    const off = subscribeDesktopUpdater(listener);
    expect(updater.onStateChange).toHaveBeenCalledWith(listener);
    off();
    expect(unsubscribe).toHaveBeenCalled();
  });
});
