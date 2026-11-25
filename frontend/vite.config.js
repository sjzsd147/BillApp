var __assign = (this && this.__assign) || function () {
    __assign = Object.assign || function(t) {
        for (var s, i = 1, n = arguments.length; i < n; i++) {
            s = arguments[i];
            for (var p in s) if (Object.prototype.hasOwnProperty.call(s, p))
                t[p] = s[p];
        }
        return t;
    };
    return __assign.apply(this, arguments);
};
import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
import fs from 'fs';
import path from 'path';
var versionConfig = JSON.parse(fs.readFileSync(path.resolve(__dirname, '..', 'version.json'), 'utf-8'));
var appVersion = String(versionConfig.version || '0.0.0').trim();
var apiTarget = process.env.VITE_API_TARGET || 'http://127.0.0.1:8000';
var apiToken = (process.env.BILLAPP_API_TOKEN || '').trim();
var proxyPaths = ['/api', '/docs', '/openapi.json'];
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
        proxy: Object.fromEntries(proxyPaths.map(function (p) { return [
            p,
            __assign({ target: apiTarget, changeOrigin: true }, (apiToken ? { headers: { 'X-BillApp-Token': apiToken } } : {})),
        ]; })),
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
