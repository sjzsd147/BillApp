/**
 * Single-source-of-truth version synchronization utility for BillApp.
 *
 * Usage:
 *   node build/sync-version.mjs           # Sync version.json -> electron/frontend package manifests
 *   node build/sync-version.mjs 1.3.0     # Update version.json to 1.3.0 and sync all manifests
 *   node build/sync-version.mjs --check   # Verify all manifests match version.json (CI guard)
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const ROOT_DIR = path.resolve(__dirname, '..');
const VERSION_FILE = path.join(ROOT_DIR, 'version.json');

const SEMVER_RE = /^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$/;

const PACKAGE_TARGETS = [
  path.join(ROOT_DIR, 'frontend', 'package.json'),
  path.join(ROOT_DIR, 'frontend', 'package-lock.json'),
  path.join(ROOT_DIR, 'electron', 'package.json'),
  path.join(ROOT_DIR, 'electron', 'package-lock.json'),
];

export function normalizeVersion(raw) {
  const cleaned = String(raw || '').trim().replace(/^v/i, '');
  if (!SEMVER_RE.test(cleaned)) {
    throw new Error(`Invalid SemVer version "${raw}". Expected format like 1.3.0`);
  }
  return cleaned;
}

export function readVersionConfig(versionFile = VERSION_FILE) {
  const raw = fs.readFileSync(versionFile, 'utf8');
  const parsed = JSON.parse(raw);
  return normalizeVersion(parsed.version);
}

function detectEol(content) {
  return content.includes('\r\n') ? '\r\n' : '\n';
}

function writeJsonPreservingEol(filePath, obj, originalContent) {
  const eol = detectEol(originalContent);
  const trailingNewline = originalContent.endsWith('\n') ? eol : '';
  const serialized = JSON.stringify(obj, null, 2).replace(/\n/g, eol) + trailingNewline;
  fs.writeFileSync(filePath, serialized, 'utf8');
}

export function writeVersionConfig(version, versionFile = VERSION_FILE) {
  const normalized = normalizeVersion(version);
  const original = fs.existsSync(versionFile) ? fs.readFileSync(versionFile, 'utf8') : '{\n}\n';
  writeJsonPreservingEol(versionFile, { version: normalized }, original);
  return normalized;
}

export function syncVersionFiles({ targetVersion = null, checkOnly = false } = {}) {
  let desiredVersion;
  if (targetVersion) {
    desiredVersion = normalizeVersion(targetVersion);
    if (!checkOnly) {
      writeVersionConfig(desiredVersion);
    }
  } else {
    desiredVersion = readVersionConfig();
  }

  const mismatches = [];
  const updated = [];

  for (const filePath of PACKAGE_TARGETS) {
    if (!fs.existsSync(filePath)) {
      continue;
    }
    const relPath = path.relative(ROOT_DIR, filePath);
    const original = fs.readFileSync(filePath, 'utf8');
    const data = JSON.parse(original);
    let dirty = false;

    if (data.version !== desiredVersion) {
      mismatches.push(`${relPath} (version=${data.version}, expected=${desiredVersion})`);
      data.version = desiredVersion;
      dirty = true;
    }

    if (
      data.packages &&
      typeof data.packages === 'object' &&
      data.packages[''] &&
      typeof data.packages[''] === 'object' &&
      data.packages[''].version !== desiredVersion
    ) {
      mismatches.push(`${relPath} (packages[""].version=${data.packages[''].version}, expected=${desiredVersion})`);
      data.packages[''].version = desiredVersion;
      dirty = true;
    }

    if (dirty && !checkOnly) {
      writeJsonPreservingEol(filePath, data, original);
      updated.push(relPath);
    }
  }

  return {
    version: desiredVersion,
    updated,
    mismatches,
  };
}

function runCli() {
  const args = process.argv.slice(2);
  const checkOnly = args.includes('--check');
  const positional = args.find((a) => !a.startsWith('-')) || null;

  if (checkOnly && positional) {
    console.error('Error: --check cannot be combined with a positional version argument.');
    process.exit(1);
  }

  const result = syncVersionFiles({ targetVersion: positional, checkOnly });
  if (checkOnly) {
    if (result.mismatches.length > 0) {
      console.error(`Version drift detected (version.json=${result.version}):`);
      for (const item of result.mismatches) {
        console.error(`  - ${item}`);
      }
      console.error('Run "node build/sync-version.mjs" to synchronize all manifests.');
      process.exit(1);
    }
    console.log(`Version check OK: all manifests match version.json (${result.version}).`);
    return;
  }

  if (result.updated.length > 0) {
    console.log(`Synced version ${result.version} to: ${result.updated.join(', ')}`);
  } else {
    console.log(`All manifests are already at version ${result.version}.`);
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === __filename) {
  runCli();
}
