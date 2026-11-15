import apiClient from './client';
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

export function restoreDatabase(file: File): Promise<RestoreResponse> {
  const formData = new FormData();
  formData.append('file', file);
  return apiClient.post('/restore', formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
  });
}

export function resetData(payload: ResetDataRequest): Promise<ResetDataResponse> {
  return apiClient.post('/data/reset', payload);
}
