import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
import fs from 'fs';
import path from 'path';

const versionConfig = JSON.parse(
  fs.readFileSync(path.resolve(__dirname, '..', 'version.json'), 'utf-8')
) as { version: string };
const appVersion = String(versionConfig.version || '0.0.0').trim();

const apiTarget = process.env.VITE_API_TARGET || 'http://127.0.0.1:8000';
const apiToken = (process.env.BILLAPP_API_TOKEN || '').trim();
const proxyPaths = ['/api', '/docs', '/openapi.json'] as const;

export default defineConfig({
  base: './',
  plugins: [vue()],
  define: {
    __APP_VERSION__: JSON.stringify(appVersion),
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, 'src'),
    },
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: Object.fromEntries(
      proxyPaths.map((p) => [
        p,
        {
          target: apiTarget,
          changeOrigin: true,
          ...(apiToken ? { headers: { 'X-BillApp-Token': apiToken } } : {}),
        },
      ])
    ),
  },
  build: {
    outDir: 'dist',
    assetsDir: 'assets',
    sourcemap: false,
    chunkSizeWarningLimit: 1500,
  },
  // @ts-ignore vitest config
  test: {
    globals: true,
    environment: 'happy-dom',
    include: ['tests/**/*.test.ts', 'src/**/*.spec.ts'],
  },
});
