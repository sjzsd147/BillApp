/** Helpers for interacting with the Electron desktop preload bridge when present. */

export const GITHUB_RELEASES_URL = 'https://github.com/sjzsd147/BillApp/releases';

export function getDesktopBridge(): BillDesktopBridge | undefined {
  if (typeof window === 'undefined') {
    return undefined;
  }
  return window.billDesktop;
}

export function isElectronDesktop(): boolean {
  return Boolean(getDesktopBridge()?.isElectron);
}

export async function saveDatabaseBackupViaDesktop(
  defaultFilename: string
): Promise<NativeSaveResult | null> {
  const bridge = getDesktopBridge();
  if (!bridge?.saveDatabaseBackup) {
    return null;
  }
  return bridge.saveDatabaseBackup(defaultFilename);
}

export async function getDesktopUpdaterState(): Promise<DesktopUpdateState | null> {
  const updater = getDesktopBridge()?.updater;
  if (!updater?.getState) {
    return null;
  }
  return updater.getState();
}

export async function checkForUpdatesViaDesktop(): Promise<DesktopUpdateState | null> {
  const updater = getDesktopBridge()?.updater;
  if (!updater?.checkForUpdates) {
    return null;
  }
  return updater.checkForUpdates();
}

export async function downloadUpdateViaDesktop(): Promise<DesktopUpdateState | null> {
  const updater = getDesktopBridge()?.updater;
  if (!updater?.downloadUpdate) {
    return null;
  }
  return updater.downloadUpdate();
}

export async function installUpdateViaDesktop(): Promise<{ ok: boolean; error?: string } | null> {
  const updater = getDesktopBridge()?.updater;
  if (!updater?.quitAndInstall) {
    return null;
  }
  return updater.quitAndInstall();
}

export async function openReleasePageViaDesktop(): Promise<boolean> {
  const updater = getDesktopBridge()?.updater;
  if (updater?.openReleasePage) {
    const res = await updater.openReleasePage();
    return Boolean(res?.ok);
  }
  if (typeof window !== 'undefined' && typeof window.open === 'function') {
    window.open(GITHUB_RELEASES_URL, '_blank', 'noopener,noreferrer');
    return true;
  }
  return false;
}

export function subscribeDesktopUpdater(
  callback: (state: DesktopUpdateState) => void
): () => void {
  const updater = getDesktopBridge()?.updater;
  if (!updater?.onStateChange) {
    return () => {};
  }
  return updater.onStateChange(callback);
}
