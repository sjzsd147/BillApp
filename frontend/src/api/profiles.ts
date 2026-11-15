import apiClient from './client';
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

export function testProfile(
  file: File,
  yamlContent?: string,
  profileId?: string
): Promise<ProfileTestResponse> {
  const formData = new FormData();
  formData.append('file', file);
  if (yamlContent) formData.append('yaml_content', yamlContent);
  if (profileId) formData.append('profile_id', profileId);
  return apiClient.post('/profiles/test', formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
  });
}

export function suggestMapping(data: SuggestMappingRequest): Promise<SuggestMappingResponse> {
  return apiClient.post('/profiles/suggest', data, { timeout: 120000 });
}
