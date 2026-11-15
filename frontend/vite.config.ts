import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
import path from 'path';

const apiTarget = process.env.VITE_API_TARGET || 'http://127.0.0.1:8000';
const proxyPaths = ['/api', '/docs', '/openapi.json'] as const;

export default defineConfig({
  base: './',
  plugins: [vue()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, 'src'),
    },
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: Object.fromEntries(
      proxyPaths.map((p) => [p, { target: apiTarget, changeOrigin: true }])
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
