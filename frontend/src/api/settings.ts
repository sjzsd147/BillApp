import apiClient from './client';
import { MAX_BACKUP_UPLOAD_BYTES, snapshotFileToMemory } from '@/utils/uploadFile';
import type {
  ResetDataRequest,
  ResetDataResponse,
  RestoreResponse,
  SettingsOut,
  SettingsUpdateRequest,
} from '@/types/api';

export function getSettings(): Promise<SettingsOut> {
  return apiClient.get('/settings');
}

export function updateSettings(data: SettingsUpdateRequest): Promise<SettingsOut> {
  return apiClient.put('/settings', data);
}

export function backupDatabase(): Promise<Blob> {
  return apiClient.post('/backup', null, { responseType: 'blob' });
}

export async function restoreDatabase(file: File): Promise<RestoreResponse> {
  const safeFile = await snapshotFileToMemory(file, {
    maxBytes: MAX_BACKUP_UPLOAD_BYTES,
    source: 'upload',
  });
  const formData = new FormData();
  formData.append('file', safeFile);
  return apiClient.post('/restore', formData);
}

export function resetData(payload: ResetDataRequest): Promise<ResetDataResponse> {
  return apiClient.post('/data/reset', payload);
}
