'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const {
  createDesktopLogger,
  formatLocalFilenameStamp,
  formatLocalTimestamp,
  sanitizeLogText,
} = require('../src/logger.cjs');

test('sanitizeLogText redacts tokens, API keys, encrypted envelopes, and URL credentials', () => {
  const input =
    'Authorization: Bearer live_tok_999 X-BillApp-Token: abcdef123456 ' +
    'boot_token=bootxyz sk-proj-secretkey987654321 encv1:QUJDREVGR0hJSktMTU5P ' +
    'https://user:pass123@proxy.local:8080 password=topsecret';
  const cleaned = sanitizeLogText(input);

  for (const secret of [
    'live_tok_999',
    'abcdef123456',
    'bootxyz',
    'sk-proj-secretkey987654321',
    'QUJDREVGR0hJSktMTU5P',
    'user:pass123',
    'topsecret',
  ]) {
    assert.equal(cleaned.includes(secret), false, `Expected ${secret} to be redacted`);
  }
  assert.match(cleaned, /\[REDACTED\]/);
});

test('createDesktopLogger writes sanitized logs, rotates on size limit, and collects bounded files', () => {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'billapp-desktop-logger-'));
  try {
    const logger = createDesktopLogger({
      logDir: tmpDir,
      filename: 'desktop.log',
      maxBytes: 220,
      maxBackups: 2,
    });

    logger.info('Booting desktop shell with sk-live-secret1234567890');
    logger.warn('Updater warning line number one');
    logger.error(new Error('Simulated failure'));
    logger.logBackend('stderr', 'WARNING: sidecar message line A\nWARNING: sidecar message line B\n');
    logger.info('Another entry to ensure rotation happens across multiple writes');
    logger.info('Final entry after rotation for collection check');

    const info = logger.getInfo();
    assert.equal(info.logPath, path.join(tmpDir, 'desktop.log'));
    assert.ok(info.files.length >= 2, `Expected rotated backups, got ${info.files.length}`);
    assert.ok(info.files.length <= 3, `Expected at most 1 active + 2 backups, got ${info.files.length}`);
    assert.ok(info.totalSizeBytes > 0);

    const collected = logger.collectLogs();
    assert.equal(collected.length, info.files.length);
    const merged = collected.map((c) => c.content).join('\n');
    assert.equal(merged.includes('sk-live-secret1234567890'), false);
    assert.match(merged, /Final entry after rotation/);
  } finally {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
});

test('desktop log timestamps include explicit local UTC offset and main.cjs omits subtractive <-loopback>', () => {
  const sampleDate = new Date(2026, 10, 25, 21, 38, 42, 123);
  const ts = formatLocalTimestamp(sampleDate);
  assert.match(ts, /^2026-11-25 21:38:42\.123[+-]\d{2}:\d{2}$/);
  assert.equal(formatLocalFilenameStamp(sampleDate), '20261125-213842');

  const mainSrc = fs.readFileSync(path.join(__dirname, '..', 'src', 'main.cjs'), 'utf8');
  const proxyMatch = mainSrc.match(/appendSwitch\(\s*['"]proxy-bypass-list['"]\s*,\s*['"]([^'"]+)['"]\s*\)/);
  assert.ok(proxyMatch, 'Expected proxy-bypass-list commandLine switch in main.cjs');
  assert.equal(proxyMatch[1].includes('<-loopback>'), false, 'proxy-bypass-list must not include subtractive <-loopback>');
  assert.match(proxyMatch[1], /127\.0\.0\.1/);
  assert.match(mainSrc, /onErrorOccurred/);
  assert.match(mainSrc, /loopback-network/);
});
