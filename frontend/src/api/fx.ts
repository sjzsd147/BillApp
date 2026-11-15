import apiClient from './client';
import type {
  FxManualRateRequest,
  FxManualRateResponse,
  FxRatesResponse,
  FxRefreshResponse,
  TransactionItem,
} from '@/types/api';

/**
 * Retrieve FX overview: recorded rates, used currencies, and pending transaction counts.
 */
export async function getFxRates(): Promise<FxRatesResponse> {
  return apiClient.get('/fx/rates');
}

/**
 * Trigger background/online refresh for transactions with missing exchange rates.
 */
export async function refreshFx(): Promise<FxRefreshResponse> {
  return apiClient.post('/fx/refresh');
}

/**
 * Manually record/supplement an exchange rate and update matching missing transactions.
 */
export async function saveFxRate(payload: FxManualRateRequest): Promise<FxManualRateResponse> {
  return apiClient.post('/fx/rates', payload);
}

/**
 * Retry obtaining exchange rate for a single foreign currency transaction.
 */
export async function retryTxnFx(txnId: number): Promise<TransactionItem> {
  return apiClient.post(`/transactions/${txnId}/fx`);
}
