import apiClient from './client';
import type {
  ReviewItemOut,
  ReviewActionResponse,
  BulkConfirmRequest,
  BulkConfirmResponse,
} from '@/types/api';

export function getReviewList(): Promise<ReviewItemOut[]> {
  return apiClient.get('/review');
}

export function confirmReview(linkId: number): Promise<ReviewActionResponse> {
  return apiClient.post(`/review/${linkId}/confirm`);
}

export function rejectReview(linkId: number): Promise<ReviewActionResponse> {
  return apiClient.post(`/review/${linkId}/reject`);
}

export function bulkConfirmReview(linkIds: number[]): Promise<BulkConfirmResponse> {
  const payload: BulkConfirmRequest = { link_ids: linkIds };
  return apiClient.post('/review/bulk-confirm', payload);
}
