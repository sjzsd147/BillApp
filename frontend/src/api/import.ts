import apiClient from './client';
import { MAX_STATEMENT_UPLOAD_BYTES, snapshotFileToMemory } from '@/utils/uploadFile';
import type {
  ImportPreviewItem,
  ExtractRawResponse,
  CommitRequest,
  ImportCommitResponse,
  BatchItem,
  BatchRollbackResponse,
} from '@/types/api';

export async function extractRawImport(file: File, password?: string): Promise<ExtractRawResponse> {
  const safeFile = await snapshotFileToMemory(file, {
    maxBytes: MAX_STATEMENT_UPLOAD_BYTES,
    source: 'upload',
  });
  const formData = new FormData();
  formData.append('file', safeFile);
  if (password) {
    formData.append('password', password);
  }
  return apiClient.post('/import/extract-raw', formData, {
    timeout: 60000,
  });
}

export async function previewImport(
  file: File,
  password?: string,
  profileId?: string,
  useLlm?: boolean
): Promise<ImportPreviewItem> {
  const safeFile = await snapshotFileToMemory(file, {
    maxBytes: MAX_STATEMENT_UPLOAD_BYTES,
    source: 'upload',
  });
  const formData = new FormData();
  formData.append('file', safeFile);
  if (password) {
    formData.append('password', password);
  }
  if (profileId) {
    formData.append('profile_id', profileId);
  }
  if (useLlm) {
    formData.append('use_llm', 'true');
  }
  return apiClient.post('/import/preview', formData, {
    timeout: useLlm ? 300000 : 60000,
  });
}

export function commitImport(data: CommitRequest): Promise<ImportCommitResponse> {
  return apiClient.post('/import/commit', data, {
    timeout: 300000,
  });
}

export function getBatches(): Promise<BatchItem[]> {
  return apiClient.get('/import/batches');
}

export function deleteBatch(batchId: number): Promise<BatchRollbackResponse> {
  return apiClient.delete(`/import/batches/${batchId}`);
}
