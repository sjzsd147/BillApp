/** Helpers for interacting with the Electron desktop preload bridge when present. */

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
