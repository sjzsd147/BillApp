import apiClient from './client';
import type {
  CategoryItemOut,
  CategoryCreateRequest,
  CategoryUpdateRequest,
  CategoryDeleteResponse,
} from '@/types/api';

export function getCategories(direction?: 'expense' | 'income'): Promise<CategoryItemOut[]> {
  return apiClient.get('/categories', { params: { direction } });
}

export function createCategory(data: CategoryCreateRequest): Promise<CategoryItemOut> {
  return apiClient.post('/categories', data);
}

export function updateCategory(id: number, data: CategoryUpdateRequest): Promise<CategoryItemOut> {
  return apiClient.put(`/categories/${id}`, data);
}

export function deleteCategory(id: number, reassignToId?: number): Promise<CategoryDeleteResponse> {
  return apiClient.delete(`/categories/${id}`, {
    params: reassignToId ? { reassign_to_id: reassignToId } : {},
  });
}
