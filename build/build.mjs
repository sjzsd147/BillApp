/**
 * Cross-platform BillApp Desktop Build Orchestrator (Windows / macOS / Linux).
 *
 * Steps:
 *  1. Build Vue/Vite production assets -> frontend/dist
 *  2. Package Python FastAPI backend sidecar with PyInstaller -> build/sidecar/billapp-backend
 *  3. Run `--selftest` against the packaged backend sidecar
 *  4. Run electron-builder (`--dir` or installer target) -> dist/ (or --out=<dir>)
 *  5. Run `--smoke-test` on unpacked Electron app (when unpacked executable is runnable)
 */

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync, spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const rootDir = path.resolve(__dirname, '..');
const frontendDir = path.join(rootDir, 'frontend');
const electronDir = path.join(rootDir, 'electron');
const buildWorkDir = path.join(rootDir, 'build', 'work');
const sidecarDistDir = path.join(rootDir, 'build', 'sidecar');
const specFile = path.join(rootDir, 'build', 'billapp.spec');

function parseArgs(argv) {
  const opts = {
    dirOnly: argv.includes('--dir'),
    skipBackend: argv.includes('--skip-backend'),
    skipFrontend: argv.includes('--skip-frontend'),
    skipSmoke: argv.includes('--skip-smoke'),
    outDir: '',
    platformFlag: '',
    archFlag: '',
    publishMode: 'never',
  };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === '--out' && argv[i + 1]) {
      opts.outDir = path.resolve(argv[i + 1]);
      i += 1;
    } else if (argv[i].startsWith('--out=')) {
      opts.outDir = path.resolve(argv[i].slice('--out='.length));
    } else if (argv[i] === '--publish' && argv[i + 1]) {
      opts.publishMode = argv[i + 1];
      i += 1;
    } else if (argv[i].startsWith('--publish=')) {
      opts.publishMode = argv[i].slice('--publish='.length);
    } else if (argv[i] === '--win' || argv[i] === '--mac' || argv[i] === '--linux') {
      opts.platformFlag = argv[i];
    } else if (argv[i] === '--arm64' || argv[i] === '--x64' || argv[i] === '--universal') {
      opts.archFlag = argv[i];
    }
  }
  return opts;
}

function runCmd(command, args, { cwd, env = process.env } = {}) {
  const res = spawnSync(command, args, {
    cwd,
    env,
    stdio: 'inherit',
  });
  if (res.error) {
    throw res.error;
  }
  if (res.status !== 0) {
    throw new Error(`Command failed (${res.status}): ${command} ${args.join(' ')}`);
  }
}

function runNpm(args, { cwd, env = process.env } = {}) {
  const nodeDir = path.dirname(process.execPath);
  const candidates = [
    process.env.npm_execpath,
    path.join(nodeDir, 'node_modules', 'npm', 'bin', 'npm-cli.js'),
    path.join(nodeDir, '..', 'lib', 'node_modules', 'npm', 'bin', 'npm-cli.js'),
  ].filter(Boolean);
  const npmCli = candidates.find((p) => p.endsWith('.js') && fs.existsSync(p));
  if (npmCli) {
    return runCmd(process.execPath, [npmCli, ...args], { cwd, env });
  }
  if (process.platform === 'win32') {
    return runCmd(process.env.ComSpec || 'cmd.exe', ['/d', '/s', '/c', 'npm', ...args], { cwd, env });
  }
  return runCmd('npm', args, { cwd, env });
}

function resolvePython() {
  if (process.env.BILLAPP_PYTHON) return process.env.BILLAPP_PYTHON;
  const venvPy =
    process.platform === 'win32'
      ? path.join(rootDir, '.venv', 'Scripts', 'python.exe')
      : path.join(rootDir, '.venv', 'bin', 'python');
  if (fs.existsSync(venvPy)) return venvPy;
  return process.platform === 'win32' ? 'python' : 'python3';
}

function getUnpackedExecutable(distOutDir) {
  if (process.platform === 'win32') {
    return path.join(distOutDir, 'win-unpacked', 'BillApp.exe');
  }
  if (process.platform === 'darwin') {
    const arm = path.join(distOutDir, 'mac-universal', 'BillApp.app', 'Contents', 'MacOS', 'BillApp');
    const intel = path.join(distOutDir, 'mac', 'BillApp.app', 'Contents', 'MacOS', 'BillApp');
    const arm64 = path.join(distOutDir, 'mac-arm64', 'BillApp.app', 'Contents', 'MacOS', 'BillApp');
    return [arm, arm64, intel].find((p) => fs.existsSync(p)) || arm;
  }
  return ['billapp-desktop', 'billapp'].map((name) => path.join(distOutDir, 'linux-unpacked', name)).find((p) => fs.existsSync(p))
    || path.join(distOutDir, 'linux-unpacked', 'billapp-desktop');
}

function hasWindowsLowMandatoryLevel(filePath) {
  if (process.platform !== 'win32' || !fs.existsSync(filePath)) {
    return false;
  }
  try {
    const out = execFileSync('icacls', [filePath], { encoding: 'utf8', windowsHide: true });
    return out.includes('Low Mandatory Level');
  } catch {
    return false;
  }
}

function resolveWritableTempRoot() {
  if (process.platform === 'win32') {
    const localLow = path.join(os.homedir(), 'AppData', 'LocalLow');
    if (fs.existsSync(localLow)) {
      return localLow;
    }
  }
  return os.tmpdir();
}

function main() {
  const opts = parseArgs(process.argv.slice(2));
  const pythonExe = resolvePython();
  const distOutDir = opts.outDir || path.join(rootDir, 'dist');

  if (!opts.skipFrontend) {
    console.log('==> 1/5 Building Frontend (Vite production bundle)...');
    const tmpSandbox = path.join(frontendDir, 'node_modules', '.tmp');
    fs.mkdirSync(tmpSandbox, { recursive: true });
    try {
      runNpm(['run', 'build'], {
        cwd: frontendDir,
        env: { ...process.env, TEMP: tmpSandbox, TMP: tmpSandbox },
      });
    } finally {
      fs.rmSync(tmpSandbox, { recursive: true, force: true });
    }
  }

  const sidecarExeName = process.platform === 'win32' ? 'billapp-backend.exe' : 'billapp-backend';
  const sidecarExe = path.join(sidecarDistDir, 'billapp-backend', sidecarExeName);

  if (!opts.skipBackend) {
    console.log('==> 2/5 Building Python Sidecar with PyInstaller...');
    fs.rmSync(buildWorkDir, { recursive: true, force: true });
    fs.rmSync(sidecarDistDir, { recursive: true, force: true });
    runCmd(
      pythonExe,
      ['-m', 'PyInstaller', specFile, '--workpath', buildWorkDir, '--distpath', sidecarDistDir, '--noconfirm'],
      { cwd: rootDir }
    );
    if (!fs.existsSync(sidecarExe)) {
      throw new Error(`Expected backend sidecar binary not found at ${sidecarExe}`);
    }

    console.log('==> 3/5 Running Self-Test on Packaged Backend Sidecar...');
    if (hasWindowsLowMandatoryLevel(sidecarExe)) {
      console.log(
        `    Skipping direct launch of '${sidecarExe}' (inherited Windows Low Mandatory Level ACL inside workspace; build outside workspace ACLs to execute binaries directly).`
      );
    } else {
      const tmpDir = fs.mkdtempSync(path.join(resolveWritableTempRoot(), 'billapp-sidecar-test-'));
      const tempDb = path.join(tmpDir, 'selftest.db');
      try {
        runCmd(sidecarExe, ['--selftest', '--port', '0', '--db', tempDb], {
          cwd: path.dirname(sidecarExe),
        });
      } finally {
        fs.rmSync(tmpDir, { recursive: true, force: true });
      }
    }
  }

  if (!fs.existsSync(path.join(electronDir, 'node_modules'))) {
    console.log('==> Installing Electron dependencies...');
    runNpm(['install'], { cwd: electronDir });
  }

  console.log('==> 4/5 Packaging Electron Desktop App...');
  const builderArgs = ['exec', 'electron-builder', '--', '--config', 'electron-builder.yml'];
  if (opts.dirOnly) {
    builderArgs.push('--dir');
  } else if (opts.platformFlag) {
    builderArgs.push(opts.platformFlag);
  }
  if (opts.archFlag) {
    builderArgs.push(opts.archFlag);
  }
  if (opts.publishMode) {
    builderArgs.push('--publish', opts.publishMode);
  }
  // GitHub Actions expands unset secrets (e.g. CSC_LINK: ${{ secrets.CSC_LINK }}) to "".
  // On macOS, electron-builder checks `cscLink == null` rather than empty string, causing it
  // to resolve "" against electronDir and fail with `<electronDir> not a file`.
  const builderEnv = { ...process.env };
  for (const key of [
    'CSC_LINK',
    'CSC_KEY_PASSWORD',
    'CSC_NAME',
    'CSC_INSTALLER_LINK',
    'CSC_INSTALLER_KEY_PASSWORD',
    'WIN_CSC_LINK',
    'WIN_CSC_KEY_PASSWORD',
  ]) {
    if (typeof builderEnv[key] === 'string' && builderEnv[key].trim() === '') {
      delete builderEnv[key];
    }
  }

  builderArgs.push(`--config.directories.output=${distOutDir}`);
  runNpm(builderArgs, { cwd: electronDir, env: builderEnv });

  const unpackedExe = getUnpackedExecutable(distOutDir);
  if (!opts.skipSmoke && fs.existsSync(unpackedExe)) {
    if (hasWindowsLowMandatoryLevel(unpackedExe)) {
      console.log(
        `==> 5/5 Skipping Chromium --smoke-test on '${unpackedExe}' (inherited Windows Low Mandatory Level ACL). Build with --out outside this workspace or run installer.`
      );
    } else {
      console.log('==> 5/5 Running End-to-End Smoke Test on Packaged Electron App...');
      const tmpSmokeDir = fs.mkdtempSync(path.join(os.tmpdir(), 'billapp-electron-smoke-'));
      const smokeDb = path.join(tmpSmokeDir, 'smoke.db');
      try {
        execFileSync(unpackedExe, ['--smoke-test', '--db', smokeDb], {
          cwd: path.dirname(unpackedExe),
          stdio: 'inherit',
          env: { ...process.env, BILLAPP_ELECTRON_SMOKETEST: '1' },
          timeout: 45000,
        });
      } finally {
        fs.rmSync(tmpSmokeDir, { recursive: true, force: true });
      }
    }
  }

  console.log(`Build completed successfully in: ${distOutDir}`);
}

main();
