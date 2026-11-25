'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const {
  findFreePort,
  httpGetJson,
  mergeNoProxy,
  parseDbCliArg,
  resolveBackendLaunchSpec,
  resolvePackagedSidecar,
  startBackend,
} = require('../src/backend.cjs');

test('parseDbCliArg extracts --db=value and --db value formats', () => {
  assert.equal(parseDbCliArg(['electron', '.', '--db=/tmp/a.db']), '/tmp/a.db');
  assert.equal(parseDbCliArg(['electron', '.', '--db', 'D:\\data\\b.db']), 'D:\\data\\b.db');
  assert.equal(parseDbCliArg(['electron', '.']), null);
});

test('mergeNoProxy always guarantees loopback exclusions', () => {
  const merged = mergeNoProxy('example.com, 10.0.0.1');
  assert.match(merged, /127\.0\.0\.1/);
  assert.match(merged, /localhost/);
  assert.match(merged, /::1/);
  assert.match(merged, /example\.com/);
});

test('resolveBackendLaunchSpec handles packaged Win/macOS/Linux sidecars and dev launcher', () => {
  const repoRoot = path.resolve(__dirname, '..', '..');

  const winPack = resolveBackendLaunchSpec({
    isPackaged: true,
    resourcesPath: 'C:\\Program Files\\BillApp\\resources',
    repoRoot,
    port: 19001,
    frontendDir: 'C:\\Program Files\\BillApp\\resources\\frontend-dist',
    platform: 'win32',
  });
  assert.equal(winPack.command, path.join('C:\\Program Files\\BillApp\\resources', 'backend', 'billapp-backend.exe'));
  assert.ok(winPack.args.includes('--sidecar'));
  assert.ok(winPack.args.includes('--watch-stdin'));
  assert.ok(winPack.args.includes('--port'));
  assert.ok(winPack.args.includes('19001'));

  const macPack = resolveBackendLaunchSpec({
    isPackaged: true,
    resourcesPath: '/Applications/BillApp.app/Contents/Resources',
    repoRoot,
    port: 19002,
    platform: 'darwin',
  });
  assert.equal(macPack.command, resolvePackagedSidecar('/Applications/BillApp.app/Contents/Resources', 'darwin'));
  assert.ok(macPack.command.endsWith(path.join('backend', 'billapp-backend')));

  const devSpec = resolveBackendLaunchSpec({
    isPackaged: false,
    resourcesPath: '',
    repoRoot,
    port: 19003,
    dbPath: '/tmp/bill.db',
    env: { ...process.env, BILLAPP_PYTHON: '/usr/bin/python3' },
    platform: 'linux',
  });
  assert.equal(devSpec.command, '/usr/bin/python3');
  assert.equal(devSpec.args[0], path.join(repoRoot, 'backend', 'billapp', 'launcher.py'));
  assert.ok(devSpec.args.includes('--db'));
  assert.ok(devSpec.args.includes('/tmp/bill.db'));
});

test('findFreePort and startBackend / stop lifecycle enforce token auth and ephemeral port', async () => {
  const freePort = await findFreePort(19200);
  assert.ok(freePort >= 19200);
  const ephemeralPort = await findFreePort(0);
  assert.ok(ephemeralPort > 0);

  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'billapp-electron-test-'));
  const dbPath = path.join(tmpDir, 'test.db');
  const repoRoot = path.resolve(__dirname, '..', '..');

  const handle = await startBackend({
    isPackaged: false,
    resourcesPath: '',
    repoRoot,
    dbPath,
    frontendDir: path.join(repoRoot, 'frontend', 'dist'),
    timeoutMs: 15000,
  });

  try {
    assert.equal(handle.isRunning(), true);
    assert.ok(typeof handle.apiToken === 'string' && handle.apiToken.length === 64);
    const health = await httpGetJson(`${handle.url}/api/health`);
    assert.equal(health.status, 'ok');
    assert.equal(fs.existsSync(dbPath), true);

    // Unauthenticated access to /api/meta must fail with HTTP 401
    await assert.rejects(() => httpGetJson(`${handle.url}/api/meta`), /HTTP 401/);

    // Authenticated POST /api/backup with X-BillApp-Token succeeds
    const { httpPostBuffer } = require('../src/backend.cjs');
    const bak = await httpPostBuffer(`${handle.url}/api/backup`, 10000, {
      'X-BillApp-Token': handle.apiToken,
    });
    assert.equal(bak.statusCode, 200);
    assert.equal(bak.buffer.subarray(0, 15).toString('utf8'), 'SQLite format 3');
  } finally {
    await handle.stop();
    assert.equal(handle.isRunning(), false);
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
});

test('resolveBackendLaunchSpec passes secrets only via env and never CLI args', () => {
  const repoRoot = path.resolve(__dirname, '..', '..');
  const spec = resolveBackendLaunchSpec({
    isPackaged: true,
    resourcesPath: '/opt/billapp/resources',
    repoRoot,
    port: 19005,
    apiToken: 'secret-token-abc',
    masterKey: 'secret-master-xyz',
    secretBackend: 'electron_safe_storage',
    platform: 'linux',
  });
  assert.equal(spec.env.BILLAPP_API_TOKEN, 'secret-token-abc');
  assert.equal(spec.env.BILLAPP_MASTER_KEY, 'secret-master-xyz');
  assert.equal(spec.env.BILLAPP_SECRET_BACKEND, 'electron_safe_storage');
  assert.equal(spec.args.join(' ').includes('secret-token-abc'), false);
  assert.equal(spec.args.join(' ').includes('secret-master-xyz'), false);
});

test('startBackend rejects cleanly on missing executable without uncaught error event (F-H1)', async () => {
  const repoRoot = path.resolve(__dirname, '..', '..');
  await assert.rejects(
    () =>
      startBackend({
        isPackaged: true,
        resourcesPath: path.join(os.tmpdir(), 'billapp-nonexistent-resources-dir'),
        repoRoot,
        timeoutMs: 3000,
      }),
    /Backend process exited prematurely/
  );
});
