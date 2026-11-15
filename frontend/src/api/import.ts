import apiClient from './client';
import type {
  ImportPreviewItem,
  ExtractRawResponse,
  CommitRequest,
  ImportCommitResponse,
  BatchItem,
  BatchRollbackResponse,
} from '@/types/api';

export function extractRawImport(file: File, password?: string): Promise<ExtractRawResponse> {
  const formData = new FormData();
  formData.append('file', file);
  if (password) {
    formData.append('password', password);
  }
  return apiClient.post('/import/extract-raw', formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 60000,
  });
}

export function previewImport(
  file: File,
  password?: string,
  profileId?: string,
  useLlm?: boolean
): Promise<ImportPreviewItem> {
  const formData = new FormData();
  formData.append('file', file);
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
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: useLlm ? 120000 : 60000,
  });
}

export function commitImport(data: CommitRequest): Promise<ImportCommitResponse> {
  return apiClient.post('/import/commit', data);
}

export function getBatches(): Promise<BatchItem[]> {
  return apiClient.get('/import/batches');
}

export function deleteBatch(batchId: number): Promise<BatchRollbackResponse> {
  return apiClient.delete(`/import/batches/${batchId}`);
}
