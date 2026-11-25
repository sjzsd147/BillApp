import { defineStore } from 'pinia';
import { ref } from 'vue';
import { getSystemMeta } from '@/api/meta';
import { getLLMStatus } from '@/api/llm';
import { getSettings } from '@/api/settings';
import {
  checkForUpdatesViaDesktop,
  downloadUpdateViaDesktop,
  getDesktopUpdaterState,
  installUpdateViaDesktop,
  openReleasePageViaDesktop,
  subscribeDesktopUpdater,
} from '@/utils/desktop';
import type { CategoryItemOut, GeneralSettings, MetaAccount, LLMStatusResponse } from '@/types/api';

export const useAppStore = defineStore('app', () => {
  const version = ref('1.2.3');
  const dbPath = ref('');
  const categories = ref<CategoryItemOut[]>([]);
  const accounts = ref<MetaAccount[]>([]);
  const pendingReviewCount = ref(0);
  const initialized = ref(false);
  const loading = ref(false);

  // Global search input state
  const globalSearchQuery = ref('');

  // Global UI preferences synced from /api/settings (S-11, B-03)
  const amountPrivacy = ref(false);
  const tableDensity = ref<'standard' | 'compact'>('standard');
  const learningSuggestionsEnabled = ref(true);

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

  function applyGeneralSettings(general?: Partial<GeneralSettings> | null) {
    if (!general) return;
    if (typeof general.amount_privacy === 'boolean') {
      amountPrivacy.value = general.amount_privacy;
    }
    if (general.table_density === 'compact' || general.table_density === 'standard') {
      tableDensity.value = general.table_density;
    }
    if (typeof general.learning_suggestions_enabled === 'boolean') {
      learningSuggestionsEnabled.value = general.learning_suggestions_enabled;
    }
  }

  async function fetchGeneralSettings() {
    try {
      const settings = await getSettings();
      applyGeneralSettings(settings.general);
    } catch (e) {
      console.error('Failed to load general settings', e);
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
    amountPrivacy,
    tableDensity,
    learningSuggestionsEnabled,
    llmStatus,
    updateState,
    fetchMeta,
    fetchGeneralSettings,
    applyGeneralSettings,
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
