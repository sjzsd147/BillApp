import { defineStore } from 'pinia';
import { ref } from 'vue';
import { previewImport, commitImport, getBatches, deleteBatch } from '@/api/import';
import { recordFrontendLog } from '@/utils/logCollector';
import { FileSnapshotError, getFileExtension, isMemoryBackedFile } from '@/utils/uploadFile';
import type { ImportPreviewItem, BatchItem, ImportCommitResponse } from '@/types/api';

export interface FileQueueItem {
  id: string;
  file: File | null;
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

  function addErrorItem(name: string, size: number, errorMsg: string) {
    queue.value.push({
      id: `${name}_${Date.now()}_${Math.random().toString(36).substring(2, 6)}`,
      file: null,
      name,
      size,
      status: 'error',
      errorMsg,
    });
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
    if (!item.file) {
      const msg = item.errorMsg || '无法读取该文件，请先保存到本地文件夹后再重新选择';
      item.status = 'error';
      item.errorMsg = msg;
      throw { code: 'FILE_UNREADABLE', message: msg, toasted: false };
    }

    const ext = getFileExtension(item.name);
    const backing = isMemoryBackedFile(item.file) ? 'mem' : 'disk';
    const startedAt = Date.now();
    item.status = 'parsing';
    item.errorMsg = undefined;
    const effectiveProfileId = profileId ?? item.profileId;
    if (profileId) {
      item.profileId = profileId;
    }
    recordFrontendLog(
      'info',
      'import',
      `preview start ext=${ext} size=${item.size}B backing=${backing} hasPassword=${Boolean(password)} profileId=${effectiveProfileId || '-'} useLlm=${Boolean(useLlm)}`
    );
    try {
      const preview = await previewImport(item.file, password, effectiveProfileId, useLlm);
      const durationMs = Date.now() - startedAt;
      item.preview = preview;
      if (preview.need_password) {
        item.status = 'need_password';
      } else if (preview.need_mapping) {
        item.status = 'need_mapping';
      } else {
        item.status = 'ready';
        activePreview.value = preview;
      }
      recordFrontendLog(
        'info',
        'import',
        `preview done status=${item.status} source=${preview.source || '-'} rows=${preview.total_rows ?? 0} warnings=${preview.warnings?.length ?? 0} (${durationMs}ms)`
      );
      return preview;
    } catch (err: any) {
      const durationMs = Date.now() - startedAt;
      const errMsg = err instanceof FileSnapshotError ? err.userMessage : err?.message || '解析失败';
      const errCode = err?.code || (err instanceof FileSnapshotError ? err.code : 'UNKNOWN');
      item.status = 'error';
      item.errorMsg = errMsg;
      recordFrontendLog(
        'error',
        'import',
        `preview failed ext=${ext} size=${item.size}B code=${errCode} (${durationMs}ms) ${errMsg}`
      );
      throw err;
    }
  }

  async function commitActivePreview(token: string, overrides?: Record<string, any>): Promise<ImportCommitResponse> {
    const startedAt = Date.now();
    recordFrontendLog(
      'info',
      'import',
      `commit start hasOverrides=${Boolean(overrides && Object.keys(overrides).length > 0)}`
    );
    try {
      const res = await commitImport({ token, account_overrides: overrides });
      recordFrontendLog(
        'info',
        'import',
        `commit done batchId=${res.batch_id} replacedBatchId=${res.replaced_batch_id ?? '-'} imported=${res.imported_count} duplicate=${res.duplicate_count} (${Date.now() - startedAt}ms)`
      );
      // Leave activePreview advancement to removeQueueItem() when the item is in the queue;
      // otherwise clear it immediately if it matches the committed token.
      const inQueue = queue.value.some((i) => i.preview?.token === token);
      if (!inQueue && activePreview.value?.token === token) {
        activePreview.value = null;
      }
      return res;
    } catch (err: any) {
      recordFrontendLog(
        'error',
        'import',
        `commit failed code=${err?.code || 'UNKNOWN'} (${Date.now() - startedAt}ms) ${err?.message || '导入提交失败'}`
      );
      throw err;
    }
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
    recordFrontendLog('info', 'import', `rollback batch start batchId=${batchId}`);
    try {
      await deleteBatch(batchId);
      batches.value = batches.value.filter((b) => b.id !== batchId);
      recordFrontendLog('info', 'import', `rollback batch done batchId=${batchId}`);
    } catch (err: any) {
      recordFrontendLog(
        'error',
        'import',
        `rollback batch failed batchId=${batchId} code=${err?.code || 'UNKNOWN'} ${err?.message || '撤销批次失败'}`
      );
      throw err;
    }
  }

  return {
    queue,
    activePreview,
    batches,
    loadingBatches,
    addFiles,
    addErrorItem,
    removeQueueItem,
    clearQueue,
    parseFile,
    commitActivePreview,
    fetchBatches,
    rollbackBatch,
  };
});
