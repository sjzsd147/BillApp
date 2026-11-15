'use strict';

const fs = require('node:fs');
const http = require('node:http');
const net = require('node:net');
const path = require('node:path');
const { spawn, execFileSync } = require('node:child_process');

const READY_PREFIX = 'BILLAPP_BACKEND_READY ';

/**
 * Find an available TCP port on 127.0.0.1 starting at `startPort` (or OS ephemeral if <= 0).
 */
async function findFreePort(startPort = 18100, host = '127.0.0.1') {
  if (startPort > 0) {
    for (let offset = 0; offset < 50; offset += 1) {
      const candidate = startPort + offset;
      // eslint-disable-next-line no-await-in-loop
      const available = await canBindPort(candidate, host);
      if (available) {
        return candidate;
      }
    }
  }
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.unref();
    srv.on('error', reject);
    srv.listen(0, host, () => {
      const addr = srv.address();
      const port = addr && typeof addr === 'object' ? addr.port : 0;
      srv.close((err) => (err ? reject(err) : resolve(port)));
    });
  });
}

function canBindPort(port, host = '127.0.0.1') {
  return new Promise((resolve) => {
    const srv = net.createServer();
    srv.unref();
    srv.once('error', () => resolve(false));
    srv.listen(port, host, () => {
      srv.close(() => resolve(true));
    });
  });
}

/**
 * Extract `--db=<path>` or `--db <path>` CLI argument if provided.
 */
function parseDbCliArg(argv = process.argv) {
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg.startsWith('--db=')) {
      const val = arg.slice('--db='.length).trim();
      if (val) return val;
    } else if (arg === '--db' && i + 1 < argv.length) {
      const val = String(argv[i + 1] || '').trim();
      if (val) return val;
    }
  }
  return null;
}

/**
 * Resolve Python interpreter path for unpacked / development execution.
 */
function resolvePythonExecutable(repoRoot, env = process.env, platform = process.platform) {
  const custom = (env.BILLAPP_PYTHON || '').trim();
  if (custom) {
    return custom;
  }

  const venvCandidates =
    platform === 'win32'
      ? [
          path.join(repoRoot, '.venv', 'Scripts', 'python.exe'),
          path.join(repoRoot, 'venv', 'Scripts', 'python.exe'),
        ]
      : [
          path.join(repoRoot, '.venv', 'bin', 'python'),
          path.join(repoRoot, 'venv', 'bin', 'python'),
        ];

  for (const candidate of venvCandidates) {
    if (fs.existsSync(candidate)) {
      return candidate;
    }
  }

  return platform === 'win32' ? 'python' : 'python3';
}

/**
 * Resolve packaged sidecar binary path inside Electron's `resources/backend/`.
 */
function resolvePackagedSidecar(resourcesPath, platform = process.platform) {
  const exeName = platform === 'win32' ? 'billapp-backend.exe' : 'billapp-backend';
  const candidates = [
    path.join(resourcesPath, 'backend', exeName),
    path.join(resourcesPath, 'backend', 'billapp-backend', exeName),
  ];
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) {
      return candidate;
    }
  }
  return candidates[0];
}

/**
 * Build spawn command, args, and environment for either packaged sidecar or dev Python launcher.
 */
function resolveBackendLaunchSpec({
  isPackaged,
  resourcesPath,
  repoRoot,
  port,
  host = '127.0.0.1',
  dbPath = null,
  frontendDir = null,
  env = process.env,
  platform = process.platform,
}) {
  const args = ['--sidecar', '--watch-stdin', '--host', host, '--port', String(port)];
  if (dbPath) {
    args.push('--db', dbPath);
  }
  if (frontendDir) {
    args.push('--frontend-dir', frontendDir);
  }

  const childEnv = {
    ...env,
    PYTHONUNBUFFERED: '1',
    PYTHONIOENCODING: 'utf-8',
    NO_PROXY: mergeNoProxy(env.NO_PROXY || env.no_proxy),
    no_proxy: mergeNoProxy(env.NO_PROXY || env.no_proxy),
  };

  if (frontendDir) {
    childEnv.BILLAPP_FRONTEND_DIR = frontendDir;
  }

  if (isPackaged) {
    const command = resolvePackagedSidecar(resourcesPath, platform);
    return { command, args, env: childEnv, cwd: path.dirname(command) };
  }

  const command = resolvePythonExecutable(repoRoot, env, platform);
  const launcherScript = path.join(repoRoot, 'backend', 'billapp', 'launcher.py');
  const backendPath = path.join(repoRoot, 'backend');
  const existingPyPath = childEnv.PYTHONPATH ? `${backendPath}${path.delimiter}${childEnv.PYTHONPATH}` : backendPath;
  childEnv.PYTHONPATH = existingPyPath;

  return {
    command,
    args: [launcherScript, ...args],
    env: childEnv,
    cwd: repoRoot,
  };
}

function mergeNoProxy(existing) {
  const required = ['127.0.0.1', 'localhost', '::1'];
  const items = String(existing || '')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
  for (const host of required) {
    if (!items.includes(host)) {
      items.push(host);
    }
  }
  return items.join(',');
}

/**
 * Direct loopback HTTP GET bypassing all proxy env vars.
 */
function httpGetJson(url, timeoutMs = 1000) {
  return new Promise((resolve, reject) => {
    const req = http.get(
      url,
      { agent: false, timeout: timeoutMs },
      (res) => {
        const chunks = [];
        res.on('data', (chunk) => chunks.push(chunk));
        res.on('end', () => {
          const text = Buffer.concat(chunks).toString('utf8');
          if (res.statusCode && res.statusCode >= 200 && res.statusCode < 300) {
            try {
              resolve(JSON.parse(text));
            } catch {
              resolve({ status: 'ok', raw: text });
            }
          } else {
            reject(new Error(`HTTP ${res.statusCode}: ${text}`));
          }
        });
      }
    );
    req.on('error', reject);
    req.on('timeout', () => {
      req.destroy(new Error('HTTP health check timeout'));
    });
  });
}

/**
 * Direct loopback HTTP POST returning raw Buffer response (used for database backup).
 */
function httpPostBuffer(url, timeoutMs = 30000) {
  return new Promise((resolve, reject) => {
    const parsed = new URL(url);
    const req = http.request(
      {
        hostname: parsed.hostname,
        port: parsed.port,
        path: `${parsed.pathname}${parsed.search}`,
        method: 'POST',
        agent: false,
        timeout: timeoutMs,
      },
      (res) => {
        const chunks = [];
        res.on('data', (chunk) => chunks.push(chunk));
        res.on('end', () => {
          const buffer = Buffer.concat(chunks);
          if (res.statusCode && res.statusCode >= 200 && res.statusCode < 300) {
            resolve({
              buffer,
              headers: res.headers,
              statusCode: res.statusCode,
            });
          } else {
            reject(new Error(`HTTP ${res.statusCode}: ${buffer.toString('utf8')}`));
          }
        });
      }
    );
    req.on('error', reject);
    req.on('timeout', () => {
      req.destroy(new Error('Backend backup request timeout'));
    });
    req.end();
  });
}

async function waitForBackendReady({ port, host = '127.0.0.1', timeoutMs = 20000, isExited, getStderr }) {
  const deadline = Date.now() + timeoutMs;
  const healthUrl = `http://${host}:${port}/api/health`;
  while (Date.now() < deadline) {
    if (isExited()) {
      const stderrTail = getStderr().trim();
      throw new Error(`Backend process exited prematurely.${stderrTail ? `\n${stderrTail}` : ''}`);
    }
    try {
      // eslint-disable-next-line no-await-in-loop
      const body = await httpGetJson(healthUrl, 600);
      if (body && body.status === 'ok') {
        return body;
      }
    } catch {
      // keep polling until deadline
    }
    // eslint-disable-next-line no-await-in-loop
    await new Promise((r) => setTimeout(r, 120));
  }
  const stderrTail = getStderr().trim();
  throw new Error(`Timed out waiting for backend health at ${healthUrl}.${stderrTail ? `\n${stderrTail}` : ''}`);
}

/**
 * Terminate child process gracefully via stdin EOF + SIGTERM, escalating if needed.
 */
async function stopChildProcess(child, { gracefulMs = 2500, platform = process.platform } = {}) {
  if (!child || child.exitCode !== null || child.signalCode !== null) {
    return;
  }

  const exitPromise = new Promise((resolve) => {
    child.once('exit', () => resolve());
  });

  try {
    if (child.stdin && !child.stdin.destroyed) {
      child.stdin.end();
    }
  } catch {
    // ignore broken pipe
  }

  if (platform !== 'win32') {
    try {
      child.kill('SIGTERM');
    } catch {
      // ignore if already gone
    }
  }

  const exitedGracefully = await Promise.race([
    exitPromise.then(() => true),
    new Promise((r) => setTimeout(() => r(false), gracefulMs)),
  ]);
  if (exitedGracefully) {
    return;
  }

  // Force termination if still alive after graceful window
  if (platform === 'win32' && child.pid) {
    try {
      execFileSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], {
        stdio: 'ignore',
        windowsHide: true,
      });
    } catch {
      try {
        child.kill('SIGKILL');
      } catch {
        // ignore
      }
    }
  } else {
    try {
      child.kill('SIGKILL');
    } catch {
      // ignore
    }
  }

  await Promise.race([exitPromise, new Promise((r) => setTimeout(r, 1000))]);
}

/**
 * Spawn local BillApp backend sidecar and resolve once ready to accept traffic.
 */
async function startBackend({
  isPackaged,
  resourcesPath,
  repoRoot,
  host = '127.0.0.1',
  requestedPort = 0,
  dbPath = null,
  frontendDir = null,
  env = process.env,
  platform = process.platform,
  timeoutMs = 20000,
  onLog = null,
}) {
  const port = requestedPort > 0 ? requestedPort : await findFreePort(18100, host);
  const spec = resolveBackendLaunchSpec({
    isPackaged,
    resourcesPath,
    repoRoot,
    port,
    host,
    dbPath,
    frontendDir,
    env,
    platform,
  });

  const child = spawn(spec.command, spec.args, {
    cwd: spec.cwd,
    env: spec.env,
    stdio: ['pipe', 'pipe', 'pipe'],
    windowsHide: true,
  });

  let exited = false;
  let exitCode = null;
  let readyInfo = null;
  let stdoutBuf = '';
  let stderrTail = '';

  child.once('exit', (code) => {
    exited = true;
    exitCode = code;
  });

  const appendStderr = (text) => {
    stderrTail = `${stderrTail}${text}`.slice(-4000);
    if (onLog) onLog('stderr', text);
  };

  child.stderr.on('data', (chunk) => {
    appendStderr(chunk.toString('utf8'));
  });

  child.stdout.on('data', (chunk) => {
    const text = chunk.toString('utf8');
    stdoutBuf += text;
    const lines = stdoutBuf.split(/\r?\n/);
    stdoutBuf = lines.pop() || '';
    for (const line of lines) {
      if (line.startsWith(READY_PREFIX)) {
        try {
          readyInfo = JSON.parse(line.slice(READY_PREFIX.length).trim());
        } catch {
          // ignore malformed debug output
        }
      }
      if (onLog) onLog('stdout', `${line}\n`);
    }
  });

  try {
    await waitForBackendReady({
      port,
      host,
      timeoutMs,
      isExited: () => exited,
      getStderr: () => `exitCode=${exitCode}\n${stderrTail}`,
    });
  } catch (err) {
    await stopChildProcess(child, { gracefulMs: 500, platform });
    throw err;
  }

  const effectivePort = (readyInfo && Number(readyInfo.port)) || port;
  const baseUrl = `http://${host}:${effectivePort}`;

  let stoppingPromise = null;
  const stop = async () => {
    if (!stoppingPromise) {
      stoppingPromise = stopChildProcess(child, { platform });
    }
    return stoppingPromise;
  };

  return {
    host,
    port: effectivePort,
    url: baseUrl,
    dbPath: (readyInfo && readyInfo.db_path) || dbPath,
    frontendDist: (readyInfo && readyInfo.frontend_dist) || frontendDir,
    child,
    stop,
    isRunning: () => !exited,
  };
}

module.exports = {
  READY_PREFIX,
  findFreePort,
  httpGetJson,
  httpPostBuffer,
  mergeNoProxy,
  parseDbCliArg,
  resolveBackendLaunchSpec,
  resolvePackagedSidecar,
  resolvePythonExecutable,
  startBackend,
  stopChildProcess,
};
