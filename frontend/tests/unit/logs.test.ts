import { afterEach, describe, expect, it, vi } from 'vitest';
import { createApp, h } from 'vue';
import apiClient from '@/api/client';
import { exportLogs, getLogsInfo } from '@/api/logs';
import {
  clearFrontendLogs,
  getFrontendLogs,
  installGlobalErrorCapture,
  recordFrontendLog,
  sanitizeFrontendLogText,
} from '@/utils/logCollector';

describe('frontend log collector and logs API', () => {
  afterEach(() => {
    clearFrontendLogs();
    vi.restoreAllMocks();
  });

  it('redacts sensitive secrets from frontend log entries and bounds buffer capacity', () => {
    const cleaned = sanitizeFrontendLogText(
      'Bearer secret_tok_123 sk-live-secret1234567890 X-BillApp-Token: tok999 boot_token=boot1'
    );
    expect(cleaned).not.toContain('secret_tok_123');
    expect(cleaned).not.toContain('sk-live-secret1234567890');
    expect(cleaned).not.toContain('tok999');
    expect(cleaned).not.toContain('boot1');

    for (let i = 0; i < 230; i += 1) {
      recordFrontendLog('error', 'test', `msg-${i} sk-proj-shouldnotleak9999`);
    }
    const logs = getFrontendLogs();
    expect(logs.length).toBe(200);
    expect(logs[0].message).toContain('msg-30');
    expect(logs[logs.length - 1].message).toContain('msg-229');
    expect(logs[0].message).not.toContain('sk-proj-shouldnotleak9999');
  });

  it('installs Vue errorHandler to record runtime component exceptions', () => {
    const app = createApp({ render: () => h('div') });
    const prevSpy = vi.fn();
    app.config.errorHandler = prevSpy;
    installGlobalErrorCapture(app);

    const err = new Error('Render crash with api_key=secret123');
    app.config.errorHandler?.(err, null, 'render function');

    expect(prevSpy).toHaveBeenCalledTimes(1);
    const logs = getFrontendLogs();
    expect(logs.length).toBe(1);
    expect(logs[0].source).toBe('vue:render function');
    expect(logs[0].message).not.toContain('secret123');
  });

  it('calls backend /logs/info and /logs/export endpoints with expected options', async () => {
    const getSpy = vi.spyOn(apiClient, 'get').mockResolvedValue({
      log_dir: '/tmp',
      active_log_path: '/tmp/launcher.log',
      files: [],
      total_size_bytes: 0,
      tail_lines: [],
      has_logs: false,
    } as any);
    const postSpy = vi.spyOn(apiClient, 'post').mockResolvedValue(new Blob(['ok']) as any);

    await getLogsInfo(150, true);
    expect(getSpy).toHaveBeenCalledWith('/logs/info', {
      params: { tail_lines: 150, redact_pii: true },
    });

    await exportLogs({
      format: 'zip',
      include_diagnostics: true,
      redact_pii: true,
      frontend_logs: [],
    });
    expect(postSpy).toHaveBeenCalledWith(
      '/logs/export',
      {
        format: 'zip',
        include_diagnostics: true,
        redact_pii: true,
        frontend_logs: [],
      },
      {
        responseType: 'blob',
        timeout: 60000,
      }
    );
  });
});
