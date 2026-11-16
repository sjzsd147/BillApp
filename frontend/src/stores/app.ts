import { defineStore } from 'pinia';
import { ref } from 'vue';
import { getSystemMeta } from '@/api/meta';
import { getLLMStatus } from '@/api/llm';
import {
  checkForUpdatesViaDesktop,
  downloadUpdateViaDesktop,
  getDesktopUpdaterState,
  installUpdateViaDesktop,
  openReleasePageViaDesktop,
  subscribeDesktopUpdater,
} from '@/utils/desktop';
import type { CategoryItemOut, MetaAccount, LLMStatusResponse } from '@/types/api';

export const useAppStore = defineStore('app', () => {
  const version = ref('1.2.0');
  const dbPath = ref('');
  const categories = ref<CategoryItemOut[]>([]);
  const accounts = ref<MetaAccount[]>([]);
  const pendingReviewCount = ref(0);
  const initialized = ref(false);
  const loading = ref(false);

  // Global search input state
  const globalSearchQuery = ref('');

  // Global LLM status
  const llmStatus = ref<LLMStatusResponse | null>(null);

  // Desktop auto-updater state
  const updateState = ref<DesktopUpdateState | null>(null);
  let updaterInitialized = false;
  let unsubscribeUpdater: (() => void) | null = null;

  async function fetchMeta() {
    loading.value = true;
    try {
      const meta = await getSystemMeta();
      version.value = meta.version;
      dbPath.value = meta.db_path;
      categories.value = meta.categories;
      accounts.value = meta.accounts;
      pendingReviewCount.value = meta.pending_review_count;
      initialized.value = true;
    } catch (e) {
      console.error('Failed to load system meta', e);
    } finally {
      loading.value = false;
    }
  }

  async function fetchLLMStatus() {
    try {
      llmStatus.value = await getLLMStatus();
    } catch {
      llmStatus.value = {
        configured: false,
        enabled: false,
        base_url: '',
        model: '',
        cache_count: 0,
      };
    }
  }

  async function initDesktopUpdater() {
    if (updaterInitialized) {
      return updateState.value;
    }
    updaterInitialized = true;
    unsubscribeUpdater = subscribeDesktopUpdater((nextState) => {
      updateState.value = nextState;
    });
    try {
      const current = await getDesktopUpdaterState();
      if (current) {
        updateState.value = current;
      }
    } catch (e) {
      console.error('Failed to read desktop updater state', e);
    }
    return updateState.value;
  }

  async function checkForUpdates() {
    const next = await checkForUpdatesViaDesktop();
    if (next) {
      updateState.value = next;
    }
    return next;
  }

  async function downloadUpdate() {
    const next = await downloadUpdateViaDesktop();
    if (next) {
      updateState.value = next;
    }
    return next;
  }

  async function quitAndInstallUpdate() {
    return installUpdateViaDesktop();
  }

  async function openReleasePage() {
    return openReleasePageViaDesktop();
  }

  function disposeDesktopUpdater() {
    if (unsubscribeUpdater) {
      unsubscribeUpdater();
      unsubscribeUpdater = null;
    }
    updaterInitialized = false;
  }

  function setPendingReviewCount(count: number) {
    pendingReviewCount.value = count;
  }

  function decrementReviewCount() {
    if (pendingReviewCount.value > 0) {
      pendingReviewCount.value--;
    }
  }

  return {
    version,
    dbPath,
    categories,
    accounts,
    pendingReviewCount,
    initialized,
    loading,
    globalSearchQuery,
    llmStatus,
    updateState,
    fetchMeta,
    fetchLLMStatus,
    initDesktopUpdater,
    checkForUpdates,
    downloadUpdate,
    quitAndInstallUpdate,
    openReleasePage,
    disposeDesktopUpdater,
    setPendingReviewCount,
    decrementReviewCount,
  };
});
