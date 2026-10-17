import { fileURLToPath } from 'node:url';
import { join, resolve } from 'node:path';
import { defineConfig } from '@playwright/test';

const repositoryRoot = fileURLToPath(new URL('../', import.meta.url));
const output = resolve(process.env.TRACEDESK_E2E_OUTPUT_DIR ?? 'test-results');
const python = process.env.TRACEDESK_E2E_PYTHON ?? 'python';
const database = process.env.TRACEDESK_E2E_DATABASE ?? join(repositoryRoot, '.tracedesk', 'e2e.db');
const apiPort = process.env.TRACEDESK_E2E_API_PORT ?? '8765';
const webPort = process.env.TRACEDESK_E2E_WEB_PORT ?? '15173';
const useVite = process.env.TRACEDESK_E2E_VITE === '1';
const apiUrl = `http://127.0.0.1:${apiPort}`;
const webUrl = useVite ? `http://127.0.0.1:${webPort}` : apiUrl;
const channel = process.env.TRACEDESK_E2E_CHANNEL;

function quoted(value: string): string {
  if (/["\r\n]/.test(value)) throw new Error('测试命令参数不能包含引号或换行。');
  return '"' + value + '"';
}

export default defineConfig({
  testDir: './e2e',
  outputDir: join(output, 'artifacts'),
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 45_000,
  expect: { timeout: 10_000 },
  reporter: [
    ['list'],
    ['html', { outputFolder: join(output, 'html'), open: 'never' }],
    ['json', { outputFile: join(output, 'results.json') }],
  ],
  use: {
    baseURL: webUrl,
    headless: true,
    viewport: { width: 1440, height: 1000 },
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
    reducedMotion: 'reduce',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [{ name: 'chromium', use: { browserName: 'chromium', ...(channel ? { channel } : {}) } }],
  webServer: [{
    command: `${quoted(python)} -m tracedesk.cli serve --database ${quoted(database)} --port ${apiPort}`,
    cwd: repositoryRoot,
    url: apiUrl + '/api/info',
    reuseExistingServer: false,
    timeout: 60_000,
    gracefulShutdown: { signal: 'SIGTERM', timeout: 5_000 },
    stdout: 'pipe',
    stderr: 'pipe',
    env: { PYTHONPATH: join(repositoryRoot, 'src'), PYTHONDONTWRITEBYTECODE: '1', TRACEDESK_PROVIDER: 'extractive' },
  }, ...(useVite ? [{
    command: `node node_modules/vite/bin/vite.js --host 127.0.0.1 --port ${webPort}`,
    cwd: join(repositoryRoot, 'web'),
    url: webUrl,
    reuseExistingServer: false,
    timeout: 60_000,
    env: { TRACEDESK_DEV_API_URL: apiUrl },
  }] : [])],
});
