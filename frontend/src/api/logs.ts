import apiClient from './client';
import type { LogExportRequest, LogsInfoOut } from '@/types/api';

export function getLogsInfo(tailLines = 120, redactPii = true): Promise<LogsInfoOut> {
  return apiClient.get('/logs/info', {
    params: {
      tail_lines: tailLines,
      redact_pii: redactPii,
    },
  });
}

export function exportLogs(payload: LogExportRequest): Promise<Blob> {
  return apiClient.post('/logs/export', payload, {
    responseType: 'blob',
    timeout: 60000,
  });
}
