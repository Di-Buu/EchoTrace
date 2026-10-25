// Real browser + local Auth/API/Worker; deterministic model, no cloud endpoints.
import { createRequire } from 'node:module';
import { writeFile } from 'node:fs/promises';
import path from 'node:path';
const root = process.cwd();
const require = createRequire(path.join(root, 'apps/mobile/package.json'));
const { chromium, expect } = require('@playwright/test');
const directory = process.env.UI_RESULT_DIR;
const checks = [];
async function passed(name) {
  checks.push({ check: name, passed: true });
  await writeFile(path.join(directory, 'checks.json'), JSON.stringify(checks, null, 2));
  console.log('PASS', name);
}
const browser = await chromium.launch({ channel: 'msedge', headless: true });
const context = await browser.newContext({ viewport: { width: 430, height: 932 } });
await context.route('**/*', route => {
  const url = new URL(route.request().url());
  return ['127.0.0.1', 'localhost'].includes(url.hostname) || !url.protocol.startsWith('http')
    ? route.continue() : route.abort();
});
const page = await context.newPage();
const api = 'http://127.0.0.1:18000';
const control = async values => {
  const response = await fetch('http://127.0.0.1:8089/__control', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(values),
  });
  if (!response.ok) throw new Error('Local model control unavailable');
};
try {
  await page.goto('http://127.0.0.1:8081/');
  await page.getByPlaceholder('邮箱').fill(process.env.UI_TEST_EMAIL);
  await page.getByPlaceholder('密码').fill(process.env.UI_TEST_PASSWORD);
  await page.getByText('登录', { exact: true }).click();
  await expect(page.getByText('留一下', { exact: true })).toBeVisible();
  await passed('browser_login_local_supabase');
  const content = '本地界面验收：昨天买了一个蓝色水杯。';
  await page.getByPlaceholder('写点什么…').fill(content);
  const saved = page.waitForResponse(r => r.url() === api + '/moments' && r.request().method() === 'POST');
  await page.getByLabel('发送').click();
  const response = await saved;
  if (response.status() !== 200) throw new Error('Moment HTTP save failed');
  const moment = await response.json();
  await expect(page.getByText('树洞收到了', { exact: true }).first()).toBeVisible();
  await passed('browser_save_and_feedback');
  await page.getByText('时刻', { exact: true }).click();
  await expect(page.getByText(content, { exact: true })).toBeVisible();
  await passed('browser_history_contains_saved_record');
  // Wait for real durable indexing, not a browser mock.
  for (let i = 0; i < 90; i++) {
    const result = await fetch(`http://127.0.0.1:55321/rest/v1/moment_index_chunks?select=chunk_index&moment_id=eq.${moment.id}`, {
      headers: { apikey: process.env.UI_PUBLIC_KEY, Authorization: `Bearer ${process.env.UI_TEST_TOKEN}` },
    });
    if (result.ok && (await result.json()).length) break;
    if (i === 89) throw new Error('UI fixture indexing did not finish');
    await new Promise(r => setTimeout(r, 1000));
  }
  await page.goto('http://127.0.0.1:8081/');
  await page.getByText('聊一会儿', { exact: true }).click();
  await page.getByPlaceholder('写点什么…').fill('我想聊聊今天的一件小事。');
  const chatted = page.waitForResponse(r => r.url() === api + '/chat' && r.request().method() === 'POST');
  await page.getByLabel('发送').click();
  if ((await chatted).status() !== 200) throw new Error('Companion HTTP flow failed');
  await expect(page.getByText('收到合成测试消息。', { exact: true })).toBeVisible();
  await passed('browser_companion_thread_response');
  await page.goto('http://127.0.0.1:8081/insights');
  await page.getByText('我想知道', { exact: true }).click();
  const ask = async text => {
    const input = page.getByPlaceholder('例如：过去几个月，我对工作的想法有变化吗？');
    await input.fill(text);
    const submitted = page.waitForResponse(r => r.url() === api + '/insights/query' && r.request().method() === 'POST');
    await input.locator('..').locator('[tabindex="0"]').click();
    if ((await submitted).status() !== 202) throw new Error('Analysis was not submitted asynchronously');
  };
  await ask('回顾我的水杯记录');
  await expect(page.getByText('查看进度', { exact: true })).toBeVisible();
  await passed('browser_analysis_202_and_progress');
  await page.getByText('停止整理', { exact: true }).click();
  await expect(page.getByText('整理已停止，或来源记录已变化。', { exact: true })).toBeVisible();
  await passed('browser_analysis_cancel');
  await ask('再次核对我的水杯记录');
  await expect(page).toHaveURL(/\/insight\//, { timeout: 90000 });
  await expect(page.getByText(content, { exact: true }).first()).toBeVisible();
  await passed('browser_analysis_result_with_source_text');
  await page.screenshot({ path: path.join(directory, 'insight.png'), fullPage: true });
  await page.goto('http://127.0.0.1:8081/insights');
  await page.getByText('我想知道', { exact: true }).click();
  await control({ chat_usage_tokens: 40001 });
  await ask('检查本地预算提示');
  await expect(page.getByText('这次分析已达到内容处理上限，尚未生成洞察。可以缩小问题范围后再试。', { exact: true })).toBeVisible({ timeout: 45000 });
  await passed('browser_specific_budget_failure_notice');
  await page.screenshot({ path: path.join(directory, 'budget-notice.png'), fullPage: true });
} catch (error) {
  await writeFile(path.join(directory, 'failure.txt'), String(error));
  await writeFile(path.join(directory, 'page-text.txt'), await page.locator('body').innerText());
  await page.screenshot({ path: path.join(directory, 'failure.png'), fullPage: true });
  throw error;
} finally {
  await control({ chat_usage_tokens: 10 });
  await browser.close();
}
