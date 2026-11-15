import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  getDesktopBridge,
  isElectronDesktop,
  saveDatabaseBackupViaDesktop,
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
    const res = await saveDatabaseBackupViaDesktop('backup.db');
    expect(res).toBeNull();
  });

  it('delegates database backup save dialog when running in Electron', async () => {
    const saveDatabaseBackup = vi.fn().mockResolvedValue({
      ok: true,
      cancelled: false,
      path: '/tmp/backup.db',
      filename: 'backup.db',
      size_bytes: 4096,
    });
    window.billDesktop = {
      isElectron: true,
      platform: 'darwin',
      saveDatabaseBackup,
    };

    expect(isElectronDesktop()).toBe(true);
    const res = await saveDatabaseBackupViaDesktop('backup.db');
    expect(saveDatabaseBackup).toHaveBeenCalledWith('backup.db');
    expect(res?.ok).toBe(true);
    expect(res?.path).toBe('/tmp/backup.db');
  });
});
