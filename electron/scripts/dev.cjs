'use strict';

const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { findFreePort, resolvePythonExecutable } = require('../src/backend.cjs');

const repoRoot = path.resolve(__dirname, '..', '..');
const frontendDir = path.join(repoRoot, 'frontend');
const electronDir = path.join(repoRoot, 'electron');

function spawnNpm(args, options) {
  const nodeDir = path.dirname(process.execPath);
  const candidates = [
    process.env.npm_execpath,
    path.join(nodeDir, 'node_modules', 'npm', 'bin', 'npm-cli.js'),
    path.join(nodeDir, '..', 'lib', 'node_modules', 'npm', 'bin', 'npm-cli.js'),
  ].filter(Boolean);
  const npmCli = candidates.find((p) => p.endsWith('.js') && fs.existsSync(p));
  if (npmCli) {
    return spawn(process.execPath, [npmCli, ...args], options);
  }
  if (process.platform === 'win32') {
    return spawn(process.env.ComSpec || 'cmd.exe', ['/d', '/s', '/c', 'npm', ...args], options);
  }
  return spawn('npm', args, options);
}

function waitForUrl(url, timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const attempt = () => {
      const req = http.get(url, { agent: false, timeout: 800 }, (res) => {
        res.resume();
        if (res.statusCode && res.statusCode < 500) {
          resolve();
        } else if (Date.now() < deadline) {
          setTimeout(attempt, 200);
        } else {
          reject(new Error(`Timed out waiting for ${url} (status=${res.statusCode})`));
        }
      });
      req.on('error', () => {
        if (Date.now() < deadline) {
          setTimeout(attempt, 200);
        } else {
          reject(new Error(`Timed out waiting for ${url}`));
        }
      });
      req.end();
    };
    attempt();
  });
}

async function main() {
  const backendPort = Number(process.env.BILLAPP_BACKEND_PORT || 0) || (await findFreePort(18100));
  const pythonExe = resolvePythonExecutable(repoRoot);
  const electronBin = require('electron');

  console.log(`[dev] Starting backend on 127.0.0.1:${backendPort} (${pythonExe})...`);
  const backendProc = spawn(
    pythonExe,
    [
      path.join(repoRoot, 'backend', 'billapp', 'launcher.py'),
      '--no-browser',
      '--watch-stdin',
      '--port',
      String(backendPort),
    ],
    {
      cwd: repoRoot,
      env: {
        ...process.env,
        PYTHONPATH: path.join(repoRoot, 'backend'),
        PYTHONUNBUFFERED: '1',
      },
      stdio: ['pipe', 'inherit', 'inherit'],
      windowsHide: true,
    }
  );

  await waitForUrl(`http://127.0.0.1:${backendPort}/api/health`);
  console.log(`[dev] Starting Vite dev server proxying /api -> http://127.0.0.1:${backendPort}...`);

  const viteProc = spawnNpm(['run', 'dev'], {
    cwd: frontendDir,
    env: {
      ...process.env,
      VITE_API_TARGET: `http://127.0.0.1:${backendPort}`,
    },
    stdio: 'inherit',
  });

  await waitForUrl('http://127.0.0.1:5173');
  console.log('[dev] Launching Electron...');

  const electronProc = spawn(electronBin, [electronDir], {
    cwd: electronDir,
    env: {
      ...process.env,
      BILLAPP_DEV_URL: 'http://127.0.0.1:5173',
      BILLAPP_BACKEND_URL: `http://127.0.0.1:${backendPort}`,
    },
    stdio: 'inherit',
  });

  const cleanup = () => {
    try {
      if (backendProc.stdin && !backendProc.stdin.destroyed) {
        backendProc.stdin.end();
      }
    } catch {
      // ignore
    }
    for (const proc of [viteProc, backendProc]) {
      if (proc && proc.exitCode === null) {
        try {
          proc.kill();
        } catch {
          // ignore
        }
      }
    }
  };

  process.on('SIGINT', cleanup);
  process.on('SIGTERM', cleanup);

  electronProc.on('exit', (code) => {
    cleanup();
    process.exit(code ?? 0);
  });
}

main().catch((err) => {
  console.error('[dev] Fatal error:', err);
  process.exit(1);
});
