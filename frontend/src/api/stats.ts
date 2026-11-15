import apiClient from './client';
import type {
  StatsSummary,
  TrendItem,
  CategoryStatsResponse,
  MerchantStatsResponse,
  HeatmapResponse,
  AccountStatsResponse,
} from '@/types/api';

export function getStatsSummary(params: Record<string, any> = {}): Promise<StatsSummary> {
  return apiClient.get('/stats/summary', { params });
}

export function getStatsTrend(params: Record<string, any> = {}): Promise<TrendItem[]> {
  return apiClient.get('/stats/trend', { params });
}

export function getCategoryStats(params: Record<string, any> = {}): Promise<CategoryStatsResponse> {
  return apiClient.get('/stats/category', { params });
}

export function getMerchantStats(params: Record<string, any> = {}): Promise<MerchantStatsResponse> {
  return apiClient.get('/stats/merchant', { params });
}

export function getSpendingHeatmap(params: Record<string, any> = {}): Promise<HeatmapResponse> {
  return apiClient.get('/stats/heatmap', { params });
}

export function getAccountStats(params: Record<string, any> = {}): Promise<AccountStatsResponse> {
  return apiClient.get('/stats/account', { params });
}
