import apiClient from './client';
import type {
  TransactionListResponse,
  TransactionListQueryParams,
  TransactionDetailResponse,
  TransactionPatchRequest,
  TransactionPatchResponse,
  BulkPatchRequest,
  BulkPatchResponse,
} from '@/types/api';

export function getTransactions(params: TransactionListQueryParams = {}): Promise<TransactionListResponse> {
  return apiClient.get('/transactions', { params });
}

export function getTransaction(id: number): Promise<TransactionDetailResponse> {
  return apiClient.get(`/transactions/${id}`);
}

export function patchTransaction(
  id: number,
  data: TransactionPatchRequest
): Promise<TransactionPatchResponse> {
  return apiClient.patch(`/transactions/${id}`, data);
}

export function bulkPatchTransactions(data: BulkPatchRequest): Promise<BulkPatchResponse> {
  return apiClient.patch('/transactions/bulk', data);
}

export function deleteTransaction(id: number): Promise<{ ok: boolean; deleted_id: number }> {
  return apiClient.delete(`/transactions/${id}`);
}

export function getExportUrl(params: Record<string, any> = {}): string {
  const base = typeof window !== 'undefined' && window.__BILL_API__ ? window.__BILL_API__ : '/api';
  const searchParams = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') {
      searchParams.append(k, String(v));
    }
  }
  return `${base}/export.xlsx?${searchParams.toString()}`;
}
