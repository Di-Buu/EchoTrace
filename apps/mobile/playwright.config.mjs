import { defineConfig } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));

function loadEnv(file) {
  if (!fs.existsSync(file)) return;
  for (const raw of fs.readFileSync(file, 'utf8').split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith('#') || !line.includes('=')) continue;
    const [key, ...rest] = line.split('=');
    if (!process.env[key]) process.env[key] = rest.join('=').trim().replace(/^['"]|['"]$/g, '');
  }
}

loadEnv(path.resolve(here, '../../evaluation/live-e2e.env'));

export default defineConfig({
  testDir: './e2e',
  timeout: 120_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  outputDir: '../../evaluation/results/playwright-artifacts',
  reporter: [
    ['list'],
    ['json', { outputFile: '../../evaluation/results/playwright-report.json' }],
  ],
  use: {
    baseURL: process.env.ECHOTRACE_WEB_URL || 'https://echotrace-web.vercel.app',
    channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge',
    headless: true,
    viewport: { width: 430, height: 932 },
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
});
