import apiClient from './client';
import type {
  RuleOut,
  RuleCreateRequest,
  RuleUpdateRequest,
  RuleTestRequest,
  RuleTestResponse,
  RuleApplyProposalRequest,
  RuleApplyProposalResponse,
  RuleSuggestionOut,
} from '@/types/api';

export function getRules(categoryId?: number, includeBuiltin = true): Promise<RuleOut[]> {
  return apiClient.get('/rules', {
    params: {
      category_id: categoryId,
      include_builtin: includeBuiltin,
    },
  });
}

export function createRule(data: RuleCreateRequest): Promise<RuleOut> {
  return apiClient.post('/rules', data);
}

export function updateRule(id: number, data: RuleUpdateRequest): Promise<RuleOut> {
  return apiClient.put(`/rules/${id}`, data);
}

export function deleteRule(id: number): Promise<{ ok: boolean; deleted_id: number }> {
  return apiClient.delete(`/rules/${id}`);
}

export function testRule(data: RuleTestRequest): Promise<RuleTestResponse> {
  return apiClient.post('/rules/test', data);
}

export function applyProposal(data: RuleApplyProposalRequest): Promise<RuleApplyProposalResponse> {
  return apiClient.post('/rules/apply-proposal', data);
}

export function getSuggestions(minCount = 3): Promise<RuleSuggestionOut[]> {
  return apiClient.get('/rules/suggestions', { params: { min_count: minCount } });
}
