import apiClient from './client';
import type {
  LLMStatusResponse,
  LLMTestRequest,
  LLMTestResponse,
  LLMModelsRequest,
  LLMModelsResponse,
  LLMClassifyRequest,
  LLMClassifyResponse,
  LLMReviewRequest,
  LLMReviewResponse,
} from '@/types/api';

export function getLLMStatus(): Promise<LLMStatusResponse> {
  return apiClient.get('/llm/status');
}

function resolveLlmHttpTimeout(timeoutSec?: number | null): number {
  const sec = Number(timeoutSec || 60);
  const clamped = Number.isFinite(sec) ? Math.min(Math.max(sec, 10), 180) : 60;
  return Math.max(30000, (clamped + 15) * 1000);
}

export function resolveLlmBatchHttpTimeout(timeoutSec?: number | null): number {
  const sec = Number(timeoutSec || 60);
  const clamped = Number.isFinite(sec) ? Math.min(Math.max(sec, 10), 180) : 60;
  // Cover single-chunk execution plus one-level timeout bisection / retry headroom
  return Math.max(120000, Math.min(600000, (clamped * 3 + 30) * 1000));
}

export function testLLM(data: LLMTestRequest): Promise<LLMTestResponse> {
  return apiClient.post('/llm/test', data, { timeout: resolveLlmHttpTimeout(data.timeout_sec) });
}

export function listLLMModels(data: LLMModelsRequest): Promise<LLMModelsResponse> {
  return apiClient.post('/llm/models', data, { timeout: resolveLlmHttpTimeout(data.timeout_sec) });
}

export function classifyWithLLM(
  data: LLMClassifyRequest,
  options?: { timeoutSec?: number | null }
): Promise<LLMClassifyResponse> {
  return apiClient.post('/llm/classify', data, {
    timeout: resolveLlmBatchHttpTimeout(options?.timeoutSec),
  });
}

export function reviewWithLLM(
  data: LLMReviewRequest,
  options?: { timeoutSec?: number | null }
): Promise<LLMReviewResponse> {
  return apiClient.post('/llm/review', data, {
    timeout: resolveLlmBatchHttpTimeout(options?.timeoutSec),
  });
}
