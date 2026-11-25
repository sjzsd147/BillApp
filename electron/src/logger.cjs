'use strict';

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const DEFAULT_MAX_BYTES = 5 * 1024 * 1024;
const DEFAULT_MAX_BACKUPS = 3;

function sanitizeLogText(raw) {
  if (!raw) {
    return '';
  }
  return String(raw)
    .replace(/(\bbearer\s+)([^\s"',;]+)/gi, '$1[REDACTED]')
    .replace(/(x-billapp-token\s*[:=]\s*)([^\s"',;]+)/gi, '$1[REDACTED]')
    .replace(/(boot_token=)([^\s&"']+)/gi, '$1[REDACTED]')
    .replace(/(billapp_session=)([^\s;"']+)/gi, '$1[REDACTED]')
    .replace(/(BILLAPP_(?:API_TOKEN|MASTER_KEY|BOOTSTRAP_TOKEN)\s*[:=]\s*)([^\s"',;]+)/gi, '$1[REDACTED]')
    .replace(/\bsk-[A-Za-z0-9_-]{12,}\b/g, 'sk-[REDACTED]')
    .replace(/\b(encv1|dpapi|b64):[A-Za-z0-9+/=_-]{12,}/g, '$1:[REDACTED]')
    .replace(/(https?:\/\/)([^/\s:@]+):([^/\s@]+)@/g, '$1[REDACTED]@')
    .replace(
      /(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|secret[_-]?key|password|passwd|pwd)\b\s*["']?\s*[:=]\s*["']?)([^\s"',}\]]+)/gi,
      '$1[REDACTED]'
    );
}

function formatArg(arg) {
  if (arg instanceof Error) {
    return arg.stack || `${arg.name}: ${arg.message}`;
  }
  if (typeof arg === 'string') {
    return arg;
  }
  if (arg === undefined) {
    return 'undefined';
  }
  try {
    return JSON.stringify(arg);
  } catch {
    return String(arg);
  }
}

function formatLocalTimestamp(date = new Date()) {
  const pad = (n, len = 2) => String(n).padStart(len, '0');
  const offsetMin = -date.getTimezoneOffset();
  const sign = offsetMin >= 0 ? '+' : '-';
  const absOffset = Math.abs(offsetMin);
  const tzHours = pad(Math.floor(absOffset / 60));
  const tzMins = pad(absOffset % 60);
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}.${pad(date.getMilliseconds(), 3)}` +
    `${sign}${tzHours}:${tzMins}`
  );
}

function formatLocalFilenameStamp(date = new Date()) {
  const pad = (n) => String(n).padStart(2, '0');
  return (
    `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}-` +
    `${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}`
  );
}

function readTailUtf8(filePath, maxBytes) {
  const stat = fs.statSync(filePath);
  const size = stat.size;
  if (size <= maxBytes) {
    return fs.readFileSync(filePath, 'utf8');
  }
  const fd = fs.openSync(filePath, 'r');
  try {
    const start = Math.max(0, size - maxBytes);
    const len = size - start;
    const buf = Buffer.alloc(len);
    fs.readSync(fd, buf, 0, len, start);
    let text = buf.toString('utf8');
    const nl = text.indexOf('\n');
    if (nl !== -1) {
      text = text.slice(nl + 1);
    }
    return `[... truncated earlier ${start} bytes ...]\n${text}`;
  } finally {
    fs.closeSync(fd);
  }
}

function createDesktopLogger({
  logDir,
  filename = 'desktop.log',
  maxBytes = DEFAULT_MAX_BYTES,
  maxBackups = DEFAULT_MAX_BACKUPS,
  mirrorConsole = false,
} = {}) {
  const safeFilename = path.basename(String(filename || 'desktop.log')).replace(/[^A-Za-z0-9._-]+/g, '_') || 'desktop.log';
  const candidateDirs = [logDir, path.join(os.tmpdir(), 'BillApp')].filter(Boolean);

  let resolvedDir = candidateDirs[0] || os.tmpdir();
  for (const dir of candidateDirs) {
    try {
      fs.mkdirSync(dir, { recursive: true });
      resolvedDir = dir;
      break;
    } catch {
      // try next candidate
    }
  }

  const logPath = path.join(resolvedDir, safeFilename);

  function rotateIfNeeded(incomingBytes) {
    try {
      if (!fs.existsSync(logPath)) {
        return;
      }
      const stat = fs.statSync(logPath);
      if (stat.size + incomingBytes <= maxBytes) {
        return;
      }
      if (maxBackups > 0) {
        const oldest = `${logPath}.${maxBackups}`;
        if (fs.existsSync(oldest)) {
          fs.unlinkSync(oldest);
        }
        for (let i = maxBackups - 1; i >= 1; i -= 1) {
          const src = `${logPath}.${i}`;
          const dst = `${logPath}.${i + 1}`;
          if (fs.existsSync(src)) {
            fs.renameSync(src, dst);
          }
        }
        fs.renameSync(logPath, `${logPath}.1`);
      } else {
        fs.truncateSync(logPath, 0);
      }
    } catch {
      // ignore rotation errors to never disrupt app execution
    }
  }

  function write(level, args) {
    const message = sanitizeLogText(args.map(formatArg).join(' '));
    const line = `${formatLocalTimestamp()} [${String(level || 'INFO').toUpperCase()}] ${message}\n`;
    const byteLen = Buffer.byteLength(line, 'utf8');
    try {
      fs.mkdirSync(resolvedDir, { recursive: true });
      rotateIfNeeded(byteLen);
      fs.appendFileSync(logPath, line, 'utf8');
    } catch {
      // swallow filesystem errors cleanly
    }
    if (mirrorConsole) {
      const fn = level === 'error' ? console.error : level === 'warn' ? console.warn : console.log;
      fn(`[desktop] ${message}`);
    }
  }

  function listLogFiles() {
    const candidates = [logPath];
    for (let i = 1; i <= maxBackups; i += 1) {
      candidates.push(`${logPath}.${i}`);
    }
    const items = [];
    for (const candidate of candidates) {
      try {
        const lstat = fs.lstatSync(candidate);
        if (lstat.isSymbolicLink() || !lstat.isFile()) {
          continue;
        }
        items.push({
          path: candidate,
          name: path.basename(candidate),
          size_bytes: lstat.size,
          modified_at: formatLocalTimestamp(lstat.mtime).slice(0, 19),
        });
      } catch {
        // missing file
      }
    }
    return items;
  }

  function getInfo() {
    const files = listLogFiles();
    const totalSizeBytes = files.reduce((acc, item) => acc + item.size_bytes, 0);
    return {
      logPath,
      logDir: resolvedDir,
      files: files.map(({ name, size_bytes, modified_at }) => ({ name, size_bytes, modified_at })),
      totalSizeBytes,
    };
  }

  function collectLogs({ maxTotalBytes = 4 * 1024 * 1024, maxFileBytes = 2 * 1024 * 1024 } = {}) {
    const files = listLogFiles();
    const collected = [];
    let remaining = maxTotalBytes;

    for (const file of files) {
      if (remaining <= 0) {
        break;
      }
      try {
        const limit = Math.min(maxFileBytes, remaining);
        const content = sanitizeLogText(readTailUtf8(file.path, limit));
        const byteCount = Buffer.byteLength(content, 'utf8');
        collected.push({
          name: file.name,
          content,
          size_bytes: byteCount,
        });
        remaining -= byteCount;
      } catch {
        // skip unreadable log file
      }
    }
    return collected;
  }

  return {
    logPath,
    logDir: resolvedDir,
    info: (...args) => write('INFO', args),
    warn: (...args) => write('WARN', args),
    error: (...args) => write('ERROR', args),
    logBackend: (stream, text) => {
      const lines = String(text || '')
        .split(/\r?\n/)
        .map((s) => s.trimEnd())
        .filter((s) => s.length > 0);
      const lvl = stream === 'stderr' ? 'WARN' : 'INFO';
      for (const line of lines) {
        write(lvl, [`[sidecar:${stream}]`, line]);
      }
    },
    getInfo,
    collectLogs,
  };
}

module.exports = {
  createDesktopLogger,
  formatLocalFilenameStamp,
  formatLocalTimestamp,
  sanitizeLogText,
};
