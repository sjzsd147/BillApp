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

export function testLLM(data: LLMTestRequest): Promise<LLMTestResponse> {
  return apiClient.post('/llm/test', data, { timeout: 60000 });
}

export function listLLMModels(data: LLMModelsRequest): Promise<LLMModelsResponse> {
  return apiClient.post('/llm/models', data, { timeout: 30000 });
}

export function classifyWithLLM(data: LLMClassifyRequest): Promise<LLMClassifyResponse> {
  return apiClient.post('/llm/classify', data, { timeout: 120000 });
}

export function reviewWithLLM(data: LLMReviewRequest): Promise<LLMReviewResponse> {
  return apiClient.post('/llm/review', data, { timeout: 120000 });
}
