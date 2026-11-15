import apiClient from './client';
import type { SystemMetaResponse, HealthResponse } from '@/types/api';

export function getHealth(): Promise<HealthResponse> {
  return apiClient.get('/health');
}

export function getSystemMeta(): Promise<SystemMetaResponse> {
  return apiClient.get('/meta');
}
