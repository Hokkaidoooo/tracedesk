import { defineConfig } from 'vite';

export default defineConfig({
  base: '/',
  build: {
    outDir: '../src/tracedesk/web',
    emptyOutDir: true,
    target: 'es2022',
    sourcemap: false,
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: { '/api': 'http://127.0.0.1:8765' },
  },
});
