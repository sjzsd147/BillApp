import { defineStore } from 'pinia';
import { ref } from 'vue';
import { previewImport, commitImport, getBatches, deleteBatch } from '@/api/import';
import type { ImportPreviewItem, BatchItem, ImportCommitResponse } from '@/types/api';

export interface FileQueueItem {
  id: string;
  file: File;
  name: string;
  size: number;
  status: 'queued' | 'parsing' | 'need_password' | 'need_mapping' | 'ready' | 'error' | 'success';
  errorMsg?: string;
  profileId?: string;
  preview?: ImportPreviewItem;
  accountOverrides?: Record<string, any>;
}

export const useImportStore = defineStore('import', () => {
  const queue = ref<FileQueueItem[]>([]);
  const activePreview = ref<ImportPreviewItem | null>(null);
  const batches = ref<BatchItem[]>([]);
  const loadingBatches = ref(false);

  function addFiles(files: FileList | File[]) {
    for (let i = 0; i < files.length; i++) {
      const f = files[i];
      queue.value.push({
        id: `${f.name}_${Date.now()}_${Math.random().toString(36).substring(2, 6)}`,
        file: f,
        name: f.name,
        size: f.size,
        status: 'queued',
      });
    }
  }

  function removeQueueItem(id: string) {
    const target = queue.value.find((item) => item.id === id);
    queue.value = queue.value.filter((item) => item.id !== id);
    if (target?.preview && activePreview.value?.token === target.preview.token) {
      const nextReady = queue.value.find((item) => item.status === 'ready' && item.preview);
      activePreview.value = nextReady?.preview || null;
    }
  }

  function clearQueue() {
    queue.value = [];
    activePreview.value = null;
  }

  async function parseFile(
    item: FileQueueItem,
    password?: string,
    profileId?: string,
    useLlm?: boolean
  ) {
    item.status = 'parsing';
    item.errorMsg = undefined;
    const effectiveProfileId = profileId ?? item.profileId;
    if (profileId) {
      item.profileId = profileId;
    }
    try {
      const preview = await previewImport(item.file, password, effectiveProfileId, useLlm);
      item.preview = preview;
      if (preview.need_password) {
        item.status = 'need_password';
      } else if (preview.need_mapping) {
        item.status = 'need_mapping';
      } else {
        item.status = 'ready';
        activePreview.value = preview;
      }
      return preview;
    } catch (err: any) {
      item.status = 'error';
      item.errorMsg = err.message || '解析失败';
      throw err;
    }
  }

  async function commitActivePreview(token: string, overrides?: Record<string, any>): Promise<ImportCommitResponse> {
    const res = await commitImport({ token, account_overrides: overrides });
    // Leave activePreview advancement to removeQueueItem() when the item is in the queue;
    // otherwise clear it immediately if it matches the committed token.
    const inQueue = queue.value.some((i) => i.preview?.token === token);
    if (!inQueue && activePreview.value?.token === token) {
      activePreview.value = null;
    }
    return res;
  }

  async function fetchBatches() {
    loadingBatches.value = true;
    try {
      batches.value = await getBatches();
    } catch (e) {
      console.error('Failed to load import batches', e);
    } finally {
      loadingBatches.value = false;
    }
  }

  async function rollbackBatch(batchId: number) {
    await deleteBatch(batchId);
    batches.value = batches.value.filter((b) => b.id !== batchId);
  }

  return {
    queue,
    activePreview,
    batches,
    loadingBatches,
    addFiles,
    removeQueueItem,
    clearQueue,
    parseFile,
    commitActivePreview,
    fetchBatches,
    rollbackBatch,
  };
});
