/// <reference types="vitest/config" />
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { Features } from 'lightningcss';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://localhost:8080', changeOrigin: false },
      '/ws': { target: 'ws://localhost:8080', ws: true },
    },
  },
  css: {
    // Vite 8 minifies CSS with Lightning CSS, which would rewrite logical properties (ms-*, me-*,
    // start-*, rounded-s-*) into physical ones keyed on :lang() for the old targets below. That
    // breaks dir="ltr" islands inside Hebrew pages, so keep them as written (as Vite 5 did).
    lightningcss: { exclude: Features.LogicalProperties },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    chunkSizeWarningLimit: 1500,
    // Vite 8 raised its default browser baseline (Chrome 111 / Safari 16.4);
    // keep the targets Vite 5 used so wall/NOC browsers keep working.
    target: ['es2020', 'edge88', 'firefox78', 'chrome87', 'safari14'],
    rolldownOptions: {
      output: {
        // Same split as the former Rollup `manualChunks` object form (removed
        // in Vite 8): a group pulls in its dependencies (zrender, scheduler,
        // the router's cookie helpers) recursively.
        codeSplitting: {
          groups: [
            { name: 'echarts', test: /[\\/]node_modules[\\/]echarts[\\/]/ },
            {
              name: 'react',
              test: /[\\/]node_modules[\\/](react|react-dom|react-router)[\\/]/,
            },
          ],
        },
      },
    },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/test-setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
  },
});
