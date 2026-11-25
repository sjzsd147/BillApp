import apiClient from './client';
import { resolveLlmBatchHttpTimeout } from './llm';
import { MAX_STATEMENT_UPLOAD_BYTES, snapshotFileToMemory } from '@/utils/uploadFile';
import type {
  ProfileSummary,
  ProfileDetail,
  ProfileCreateRequest,
  ProfileUpdateRequest,
  ProfileTestResponse,
  SuggestMappingRequest,
  SuggestMappingResponse,
} from '@/types/api';

export function getProfiles(): Promise<ProfileSummary[]> {
  return apiClient.get('/profiles');
}

export function getProfile(id: string): Promise<ProfileDetail> {
  return apiClient.get(`/profiles/${id}`);
}

export function createProfile(data: ProfileCreateRequest): Promise<ProfileSummary> {
  return apiClient.post('/profiles', data);
}

export function updateProfile(id: string, data: ProfileUpdateRequest): Promise<ProfileSummary> {
  return apiClient.put(`/profiles/${id}`, data);
}

export function deleteProfile(id: string): Promise<{ status: string; id: string }> {
  return apiClient.delete(`/profiles/${id}`);
}

export async function testProfile(
  file: File,
  yamlContent?: string,
  profileId?: string
): Promise<ProfileTestResponse> {
  const safeFile = await snapshotFileToMemory(file, {
    maxBytes: MAX_STATEMENT_UPLOAD_BYTES,
    source: 'upload',
  });
  const formData = new FormData();
  formData.append('file', safeFile);
  if (yamlContent) formData.append('yaml_content', yamlContent);
  if (profileId) formData.append('profile_id', profileId);
  return apiClient.post('/profiles/test', formData);
}

export function suggestMapping(
  data: SuggestMappingRequest,
  options?: { silent?: boolean; timeoutSec?: number | null }
): Promise<SuggestMappingResponse> {
  return apiClient.post('/profiles/suggest', data, {
    timeout: resolveLlmBatchHttpTimeout(options?.timeoutSec),
    ...(options?.silent ? { silent: true } : {}),
  } as any);
}
