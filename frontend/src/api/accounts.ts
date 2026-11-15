import apiClient from './client';
import type {
  AccountOut,
  AccountUpdateRequest,
  AccountMergeRequest,
  AccountMergeResponse,
} from '@/types/api';

export function getAccounts(): Promise<AccountOut[]> {
  return apiClient.get('/accounts');
}

export function patchAccount(id: number, data: AccountUpdateRequest): Promise<AccountOut> {
  return apiClient.patch(`/accounts/${id}`, data);
}

export function mergeAccounts(data: AccountMergeRequest): Promise<AccountMergeResponse> {
  return apiClient.post('/accounts/merge', data);
}
