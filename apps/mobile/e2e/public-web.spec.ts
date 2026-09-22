import { expect, type Page, test } from '@playwright/test';

const email = process.env.ECHOTRACE_E2E_EMAIL || '';
const password = process.env.ECHOTRACE_E2E_PASSWORD || '';

async function login(page: Page) {
  if (!email || !password) {
    throw new Error('请先在 evaluation/live-e2e.env 填写专用测试账号。');
  }
  await page.goto('/');
  const emailInput = page.getByPlaceholder('邮箱');
  if (await emailInput.isVisible()) {
    await emailInput.fill(email);
    await page.getByPlaceholder('密码').fill(password);
    await page.getByText('登录', { exact: true }).click();
  }
  await expect(page.getByText('留一下', { exact: true })).toBeVisible();
}

test.beforeEach(async ({ page }) => {
  await login(page);
  await page.getByText('留一下', { exact: true }).click();
});

test('真实保存后出现在时刻历史中', async ({ page }) => {
  const content = `网页自动测试 ${Date.now()}：今晚准备在晚饭后阅读二十分钟。`;
  const failures: string[] = [];
  page.on('requestfailed', (request) => {
    if (request.url().endsWith('/moments')) {
      failures.push(`${request.failure()?.errorText || 'unknown'} ${request.url()}`);
    }
  });

  await page.getByPlaceholder('写点什么…').fill(content);
  const responsePromise = page.waitForResponse(
    (response) => response.url().endsWith('/moments') && response.request().method() === 'POST',
    { timeout: 35_000 },
  );
  await page.getByLabel('发送').click();
  const response = await responsePromise.catch(() => null);
  if (!response) throw new Error(`浏览器没有收到 POST /moments 响应；${failures.join('; ') || '无 requestfailed 详情'}`);
  const body = (await response.text()).slice(0, 1000);
  expect(response.status(), `POST /moments response: ${body}`).toBe(200);
  await expect(page.getByText('树洞收到了', { exact: true }).first()).toBeVisible();

  await page.getByText('时刻', { exact: true }).click();
  await expect(page.getByText(content, { exact: true })).toBeVisible();
});

test('保存过程中有明确反馈且发送键被保护', async ({ page }) => {
  await page.route('**/moments', async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 1_500));
    await route.continue();
  });
  await page.getByPlaceholder('写点什么…').fill('保存状态自动测试');
  await page.getByLabel('发送').click();
  await expect(page.getByLabel('发送')).toHaveAttribute('aria-disabled', 'true');
  await expect(page.getByText('正在保存…', { exact: true })).toBeVisible({ timeout: 1_000 });
});

test('网络失败时保留输入并恢复发送能力', async ({ page }) => {
  await page.route('**/moments', (route) => route.abort('failed'));
  const content = '网络错误自动测试';
  await page.getByPlaceholder('写点什么…').fill(content);
  await page.getByLabel('发送').click();
  await expect(page.getByText('网络连接失败，请稍后重试。', { exact: true })).toBeVisible();
  await expect(page.getByPlaceholder('写点什么…')).toHaveValue(content);
  await expect(page.getByLabel('发送')).toHaveAttribute('aria-disabled', 'false');
});
