import type { App } from 'vue';
import type { FrontendLogEntry } from '@/types/api';

const MAX_ENTRIES = 200;
const MAX_MESSAGE_LEN = 1200;
const MAX_STACK_LEN = 2400;

const entries: FrontendLogEntry[] = [];
let globalHooksInstalled = false;

export function sanitizeFrontendLogText(raw: string): string {
  if (!raw) return '';
  return String(raw)
    .replace(/(\bbearer\s+)([^\s"',;]+)/gi, '$1[REDACTED]')
    .replace(/(x-billapp-token\s*[:=]\s*)([^\s"',;]+)/gi, '$1[REDACTED]')
    .replace(/(boot_token=)([^\s&"']+)/gi, '$1[REDACTED]')
    .replace(/(billapp_session=)([^\s;"']+)/gi, '$1[REDACTED]')
    .replace(/\bsk-[A-Za-z0-9_-]{12,}\b/g, 'sk-[REDACTED]')
    .replace(/\b(encv1|dpapi|b64):[A-Za-z0-9+/=_-]{12,}/g, '$1:[REDACTED]')
    .replace(/(https?:\/\/)([^/\s:@]+):([^/\s@]+)@/g, '$1[REDACTED]@')
    .replace(
      /(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|secret[_-]?key|password|passwd|pwd)\b\s*["']?\s*[:=]\s*["']?)([^\s"',}\]]+)/gi,
      '$1[REDACTED]'
    );
}

function truncateText(text: string, maxLen: number): string {
  if (text.length <= maxLen) return text;
  return `${text.slice(0, maxLen)}...(truncated)`;
}

export function recordFrontendLog(
  level: 'info' | 'warn' | 'error',
  source: string,
  message: string,
  stack?: string
): void {
  const sanitizedMsg = truncateText(sanitizeFrontendLogText(message || ''), MAX_MESSAGE_LEN);
  const sanitizedStack = stack ? truncateText(sanitizeFrontendLogText(stack), MAX_STACK_LEN) : undefined;
  entries.push({
    timestamp: new Date().toISOString(),
    level,
    source: (source || 'frontend').slice(0, 64),
    message: sanitizedMsg,
    stack: sanitizedStack,
  });
  if (entries.length > MAX_ENTRIES) {
    entries.splice(0, entries.length - MAX_ENTRIES);
  }
}

export function getFrontendLogs(): FrontendLogEntry[] {
  return entries.map((item) => ({ ...item }));
}

export function clearFrontendLogs(): void {
  entries.length = 0;
}

export function installGlobalErrorCapture(app: App): void {
  const prevHandler = app.config.errorHandler;
  app.config.errorHandler = (err, instance, info) => {
    const errObj = err instanceof Error ? err : new Error(String(err));
    recordFrontendLog('error', `vue:${info || 'runtime'}`, errObj.message || String(err), errObj.stack);
    if (typeof prevHandler === 'function') {
      prevHandler(err, instance, info);
    } else {
      console.error('[Vue error]', err);
    }
  };

  if (globalHooksInstalled || typeof window === 'undefined') {
    return;
  }
  globalHooksInstalled = true;

  window.addEventListener('error', (event: ErrorEvent) => {
    const location = event.filename ? ` (${event.filename}:${event.lineno}:${event.colno})` : '';
    recordFrontendLog(
      'error',
      'window.error',
      `${event.message || 'Uncaught window error'}${location}`,
      event.error instanceof Error ? event.error.stack : undefined
    );
  });

  window.addEventListener('unhandledrejection', (event: PromiseRejectionEvent) => {
    const reason = event.reason;
    const message =
      reason instanceof Error
        ? reason.message
        : typeof reason === 'object' && reason !== null && 'message' in reason
          ? String((reason as { message?: unknown }).message)
          : String(reason || 'Unhandled promise rejection');
    const stack = reason instanceof Error ? reason.stack : undefined;
    recordFrontendLog('error', 'window.unhandledrejection', message, stack);
  });
}
