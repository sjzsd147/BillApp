/// <reference types="vite/client" />

declare const __APP_VERSION__: string;

declare module '*.vue' {
  import type { DefineComponent } from 'vue';
  const component: DefineComponent<{}, {}, any>;
  export default component;
}

interface NativeSaveResult {
  ok: boolean;
  cancelled?: boolean;
  path?: string;
  filename?: string;
  size_bytes?: number;
  error?: string;
}

interface BillDesktopInfo {
  platform: string;
  isPackaged: boolean;
  appVersion: string;
  backendUrl?: string;
  installMode?: string;
  desktopLogPath?: string;
  desktopLogSizeBytes?: number;
  desktopLogFileCount?: number;
}

interface DesktopExportLogsOptions {
  format?: 'zip' | 'log';
  includeDiagnostics?: boolean;
  redactPii?: boolean;
  defaultFilename?: string;
  frontendLogs?: Array<{
    timestamp: string;
    level: string;
    source: string;
    message: string;
    stack?: string;
  }>;
}

type DesktopUpdateStatus =
  | 'idle'
  | 'checking'
  | 'available'
  | 'not-available'
  | 'downloading'
  | 'downloaded'
  | 'error';

interface DesktopUpdateProgress {
  percent: number;
  bytesPerSecond: number;
  transferred: number;
  total: number;
}

interface DesktopUpdateState {
  supported: boolean;
  autoInstallSupported: boolean;
  installMode: string;
  currentVersion: string;
  repositoryUrl: string;
  releasesUrl: string;
  status: DesktopUpdateStatus;
  latestVersion: string | null;
  releaseDate: string | null;
  releaseNotes: string | null;
  releaseUrl: string | null;
  downloadProgress: DesktopUpdateProgress | null;
  manualDownloadRequired: boolean;
  checkedAt: string | null;
  error: string | null;
  silent?: boolean;
}

interface DesktopUpdaterBridge {
  getState: () => Promise<DesktopUpdateState>;
  checkForUpdates: () => Promise<DesktopUpdateState>;
  downloadUpdate: () => Promise<DesktopUpdateState>;
  quitAndInstall: () => Promise<{ ok: boolean; error?: string; state?: DesktopUpdateState }>;
  openReleasePage: () => Promise<{ ok: boolean; url?: string; error?: string }>;
  onStateChange: (callback: (state: DesktopUpdateState) => void) => () => void;
}

interface BillDesktopBridge {
  isElectron: boolean;
  platform: string;
  versions?: {
    electron?: string;
    node?: string;
    chrome?: string;
  };
  saveDatabaseBackup: (defaultFilename?: string) => Promise<NativeSaveResult>;
  exportLogs?: (options?: DesktopExportLogsOptions) => Promise<NativeSaveResult>;
  getDesktopInfo?: () => Promise<BillDesktopInfo>;
  updater?: DesktopUpdaterBridge;
}

interface Window {
  __BILL_API__?: string;
  billDesktop?: BillDesktopBridge;
}
