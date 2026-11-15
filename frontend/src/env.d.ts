/// <reference types="vite/client" />

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
  getDesktopInfo?: () => Promise<BillDesktopInfo>;
}

interface Window {
  __BILL_API__?: string;
  billDesktop?: BillDesktopBridge;
}
