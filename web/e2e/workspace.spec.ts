import { mkdir } from 'node:fs/promises';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, test } from '@playwright/test';
import type { Page, Response } from '@playwright/test';
import type { CreatedSession, EvaluationReport, LoadedSession, Receipt, Turn } from '../src/api';

const POLICY_QUESTION = '签收后多久可以退货？';
const SCREENSHOT_DIRECTORY = fileURLToPath(new URL('../test-results/screenshots/', import.meta.url));

function isSessionCreation(response: Response): boolean {
  return new URL(response.url()).pathname === '/api/sessions' && response.request().method() === 'POST';
}

function isTurnResponse(response: Response): boolean {
  return /^\/api\/sessions\/[^/]+\/turns$/.test(new URL(response.url()).pathname)
    && response.request().method() === 'POST';
}

async function startSession(page: Page): Promise<CreatedSession> {
  const responsePromise = page.waitForResponse(isSessionCreation);
  await page.goto('/');
  const response = await responsePromise;
  expect(response.ok()).toBe(true);
  const session = await response.json() as CreatedSession;
  expect(session.customer.id).toBe('customer-a');
  await expect(page.locator('#message-input')).toBeEnabled();
  await expect(page.locator('#runtime-badge')).toHaveText('提取式模式');
  await expect(page.locator('#conversation-title')).toContainText(session.customer.name);
  return session;
}

async function sendMessage(page: Page, message: string): Promise<Turn> {
  await page.locator('#message-input').fill(message);
  const responsePromise = page.waitForResponse(isTurnResponse);
  await page.locator('#message-input').press('Enter');
  const response = await responsePromise;
  expect(response.ok()).toBe(true);
  const turn = await response.json() as Turn;
  expect(turn.message).toBe(message);
  await expect(page.getByTestId('assistant-message').last()).toHaveAttribute('data-turn-id', turn.id);
  await expect(page.getByTestId('answer-text').last()).toHaveText(turn.answer);
  await expect(page.locator('#message-input')).toHaveValue('');
  return turn;
}

async function saveScreenshot(page: Page, filename: string): Promise<void> {
  await mkdir(SCREENSHOT_DIRECTORY, { recursive: true });
  await page.screenshot({ path: join(SCREENSHOT_DIRECTORY, filename), fullPage: true });
}

async function expectNoHorizontalOverflow(page: Page): Promise<void> {
  const dimensions = await page.evaluate(() => ({
    viewport: window.innerWidth,
    root: document.documentElement.scrollWidth,
    body: document.body.scrollWidth,
  }));
  expect(dimensions.root).toBeLessThanOrEqual(dimensions.viewport);
  expect(dimensions.body).toBeLessThanOrEqual(dimensions.viewport);
}

test('查阅退货政策，展开原文并查看执行记录', async ({ page }) => {
  await startSession(page);
  const turn = await sendMessage(page, POLICY_QUESTION);
  expect(turn.status).toBe('answered');
  expect(turn.runtime).toBe('extractive');
  expect(turn.citations.length).toBeGreaterThan(0);
  expect(turn.trace.length).toBeGreaterThan(0);
  expect(turn.action).toBeNull();

  await page.getByTestId('assistant-message').last().locator('.reference-button').first().click();
  const citation = page.locator('.citation-card').first();
  await expect(page.getByTestId('citation-quote').first()).toHaveText(turn.citations[0].quote);
  await expect(citation).toContainText(turn.citations[0].source_id);
  await expect(citation).toContainText(`v${turn.citations[0].version}`);
  await expect(citation).toContainText(turn.citations[0].effective_from);
  await citation.locator('summary').click();
  await expect(citation).toHaveJSProperty('open', false);
  await citation.locator('summary').press('Enter');
  await expect(citation).toHaveJSProperty('open', true);

  await page.getByRole('tab', { name: /执行记录/ }).click();
  await expect(page.getByTestId('trace-event')).toHaveCount(turn.trace.length);
  await expect(page.locator('#trace-content')).toContainText(turn.run_id);
  await expectNoHorizontalOverflow(page);
});

test('核对订单与售后金额，确认申请后取得真实回执', async ({ page }) => {
  await startSession(page);
  const orderTurn = await sendMessage(page, '查询订单 TD-1001');
  expect(orderTurn.order?.id).toBe('TD-1001');
  expect(orderTurn.order?.status).toBe('delivered');
  await expect(page.getByTestId('order-card').last()).toContainText('TD-1001');

  const turn = await sendMessage(page, '申请退货 TD-1001');
  expect(turn.status).toBe('needs_confirmation');
  expect(turn.action?.state).toBe('pending');
  expect(turn.action?.order_id).toBe('TD-1001');
  expect(turn.action?.amount_minor).toBe(orderTurn.order?.amount_minor);
  expect(turn.citations.length).toBeGreaterThan(0);
  const action = turn.action!;
  const pending = page.getByTestId('refund-card').last();
  await expect(pending).toHaveAttribute('data-state', 'pending');
  await expect(pending).toContainText(new Intl.NumberFormat('zh-CN', {
    style: 'currency', currency: action.currency,
  }).format(action.amount_minor / 100));
  await expect(pending.getByRole('button', { name: '确认提交申请', exact: true })).toBeEnabled();
  await expect(pending.getByRole('button', { name: '取消申请', exact: true })).toBeEnabled();

  await page.getByTestId('assistant-message').last().locator('.reference-button').first().click();
  await expect(page.getByTestId('citation-quote').first()).toHaveText(turn.citations[0].quote);
  await pending.scrollIntoViewIfNeeded();
  await expectNoHorizontalOverflow(page);
  await saveScreenshot(page, 'desktop-pending.png');

  const responsePromise = page.waitForResponse((response) =>
    new URL(response.url()).pathname === `/api/actions/${action.id}/confirm`
    && response.request().method() === 'POST');
  await pending.getByRole('button', { name: '确认提交申请', exact: true }).click();
  const response = await responsePromise;
  expect(response.ok()).toBe(true);
  const receipt = await response.json() as Receipt;
  expect(receipt.state).toBe('submitted');
  expect(receipt.order_id).toBe(action.order_id);
  expect(receipt.amount_minor).toBe(action.amount_minor);
  expect(receipt.receipt_id.length).toBeGreaterThan(0);
  await expect(page.getByTestId('refund-card').last()).toHaveAttribute('data-state', 'submitted');
  await expect(page.getByTestId('refund-receipt').last()).toContainText(receipt.receipt_id);
  await expect(page.getByRole('button', { name: '确认提交申请', exact: true })).toHaveCount(0);
  await expect(page.getByTestId('refund-card').last()).toBeFocused();
  await saveScreenshot(page, 'desktop-submitted.png');
});

test('权限不足或无知识依据时，明确转交人工', async ({ page }) => {
  await startSession(page);
  const turn = await sendMessage(page, '帮我查询 TD-2001');
  expect(turn.status).toBe('handoff');
  expect(turn.order).toBeNull();
  expect(turn.action).toBeNull();
  expect(turn.trace.some((event) => event.status === 'error' || event.status === 'denied')).toBe(true);
  await expect(page.getByTestId('order-card')).toHaveCount(0);
  await expect(page.getByTestId('refund-card')).toHaveCount(0);
  await expect(page.getByTestId('assistant-message').last()).toContainText('建议人工处理');

  const unknown = await sendMessage(page, '解释一下量子纠缠的原理。');
  expect(unknown.status).toBe('handoff');
  expect(unknown.citations).toHaveLength(0);
  expect(unknown.order).toBeNull();
  expect(unknown.action).toBeNull();
  await expect(page.locator('#evidence-content')).toContainText('本次没有政策引用');
  await expect(page.getByTestId('assistant-message').last()).toContainText('建议人工处理');
  await expect(page.getByTestId('refund-card')).toHaveCount(0);
});

test('切换演示客户后开启独立会话，原会话仍可读取', async ({ page }) => {
  const original = await startSession(page);
  const turn = await sendMessage(page, POLICY_QUESTION);
  const creationPromise = page.waitForResponse(isSessionCreation);
  await page.getByLabel('演示身份', { exact: true }).selectOption('customer-b');
  const response = await creationPromise;
  expect(response.ok()).toBe(true);
  const other = await response.json() as CreatedSession;
  expect(other.id).not.toBe(original.id);
  expect(other.customer.id).toBe('customer-b');
  await expect(page.getByTestId('assistant-message')).toHaveCount(0);
  await expect(page.locator('#conversation-title')).toContainText(other.customer.name);

  const historyPromise = page.waitForResponse((result) =>
    new URL(result.url()).pathname === `/api/sessions/${original.id}`
    && result.request().method() === 'GET');
  await page.locator('.session-button').filter({ hasText: POLICY_QUESTION }).click();
  const historyResponse = await historyPromise;
  expect(historyResponse.ok()).toBe(true);
  const history = await historyResponse.json() as LoadedSession;
  expect(history.turns).toHaveLength(1);
  expect(history.turns[0].id).toBe(turn.id);
  await expect(page.getByTestId('assistant-message')).toHaveCount(1);
  await expect(page.getByTestId('answer-text')).toHaveText(turn.answer);
  await expect(page.getByLabel('演示身份', { exact: true })).toHaveValue('customer-a');

  const newSessionPromise = page.waitForResponse(isSessionCreation);
  await page.getByRole('button', { name: '新建会话', exact: false }).click();
  const fresh = await (await newSessionPromise).json() as CreatedSession;
  expect(fresh.id).not.toBe(original.id);
  expect(fresh.customer.id).toBe('customer-a');
  await expect(page.getByTestId('assistant-message')).toHaveCount(0);
  await expect(page.locator('#session-count')).toHaveText('3');
});

test('320 与 390 像素下可用键盘切换分区且没有横向溢出', async ({ page }) => {
  for (const width of [320, 390]) {
    await page.setViewportSize({ width, height: 844 });
    await startSession(page);
    await expectNoHorizontalOverflow(page);
    const tabs = page.locator('.panel-tabs');
    await tabs.getByRole('button', { name: '对话', exact: true }).focus();
    await page.keyboard.press('Tab');
    await expect(tabs.getByRole('button', { name: '会话', exact: true })).toBeFocused();
    await page.keyboard.press('Enter');
    await expect(page.locator('.sidebar')).toBeVisible();
    await expectNoHorizontalOverflow(page);
    await tabs.getByRole('button', { name: '对话', exact: true }).click();

    await page.locator('#message-input').fill(POLICY_QUESTION);
    await page.locator('#message-input').press('Shift+Enter');
    await expect(page.locator('#message-input')).toHaveValue(`${POLICY_QUESTION}\n`);
    await page.locator('#message-input').press('Backspace');
    const turn = await sendMessage(page, POLICY_QUESTION);
    await expectNoHorizontalOverflow(page);
    if (width === 390) await saveScreenshot(page, 'mobile-390.png');

    await page.getByTestId('assistant-message').last().locator('.reference-button').first().click();
    await expect(page.locator('.inspector')).toBeVisible();
    await expect(page.locator('.conversation')).toBeHidden();
    await expect(page.getByTestId('citation-quote').first()).toHaveText(turn.citations[0].quote);
    await expectNoHorizontalOverflow(page);
  }
});

test('回复传输中断后保留草稿，重试沿用原请求且不重复会话记录', async ({ page }) => {
  const session = await startSession(page);
  const sent: Array<{ message: string; request_id: string }> = [];
  let firstServerTurn: Turn | undefined;
  let attempts = 0;
  page.on('request', (request) => {
    if (/\/api\/sessions\/[^/]+\/turns$/.test(new URL(request.url()).pathname) && request.method() === 'POST') {
      sent.push(request.postDataJSON() as { message: string; request_id: string });
    }
  });
  await page.route('**/api/sessions/*/turns', async (route) => {
    attempts += 1;
    if (attempts !== 1) {
      await route.continue();
      return;
    }
    // 让真实服务先提交，再丢弃响应，覆盖回执丢失后的重试。
    const response = await route.fetch();
    expect(response.ok()).toBe(true);
    firstServerTurn = await response.json() as Turn;
    await route.abort('failed');
  });
  await page.locator('#message-input').fill(POLICY_QUESTION);
  await page.locator('#message-input').press('Enter');
  await expect(page.locator('#message-feedback')).toContainText('NETWORK_ERROR');
  await expect(page.locator('#message-input')).toHaveValue(POLICY_QUESTION);
  await expect(page.getByTestId('assistant-message')).toHaveCount(0);

  const retryPromise = page.waitForResponse(isTurnResponse);
  await page.getByRole('button', { name: '重试发送', exact: true }).click();
  const retryResponse = await retryPromise;
  expect(retryResponse.ok()).toBe(true);
  const replayed = await retryResponse.json() as Turn;
  expect(sent).toHaveLength(2);
  expect(sent[0].request_id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i);
  expect(sent[1].request_id).toBe(sent[0].request_id);
  expect(sent[1].message).toBe(sent[0].message);
  expect(replayed.id).toBe(firstServerTurn?.id);
  expect(replayed.run_id).toBe(firstServerTurn?.run_id);
  await expect(page.getByTestId('assistant-message')).toHaveCount(1);
  await expect(page.locator('#message-input')).toHaveValue('');
  await expect(page.locator('#message-feedback')).toBeHidden();

  const historyResponse = await page.request.get(`/api/sessions/${session.id}`, {
    headers: { Authorization: `Bearer ${session.token}` },
  });
  expect(historyResponse.ok()).toBe(true);
  const history = await historyResponse.json() as LoadedSession;
  expect(history.turns).toHaveLength(1);
  expect(history.turns[0].id).toBe(replayed.id);
});

test('评测页展示已有真实报告的范围和指标', async ({ page }) => {
  await startSession(page);
  const responsePromise = page.waitForResponse((response) =>
    new URL(response.url()).pathname === '/api/evaluation' && response.request().method() === 'GET');
  await page.locator('.rail').getByRole('button', { name: '评测', exact: true }).click();
  const response = await responsePromise;
  expect(response.ok()).toBe(true);
  const report = await response.json() as EvaluationReport;
  expect(report.scope.length).toBeGreaterThan(0);
  expect(report.dataset_sha256).toMatch(/^[0-9a-f]{64}$/i);
  expect(report.workflow.total).toBeGreaterThan(0);
  expect(report.workflow.cases.length).toBeGreaterThan(0);
  expect(report.workflow.passed).toBeGreaterThanOrEqual(0);
  expect(report.workflow.passed).toBeLessThanOrEqual(report.workflow.total);
  const methods = Object.values(report.retrieval);
  expect(methods.length).toBeGreaterThan(0);
  for (const method of methods) {
    expect(method.queries).toBeGreaterThan(0);
    expect(method.recall_at_3).toBeGreaterThanOrEqual(0);
    expect(method.recall_at_3).toBeLessThanOrEqual(1);
    expect(method.mrr).toBeGreaterThanOrEqual(0);
    expect(method.mrr).toBeLessThanOrEqual(1);
  }
  await expect(page.getByRole('heading', { name: '回归评测', exact: true })).toBeVisible();
  await expect(page.locator('.evaluation-scope')).toContainText(report.scope);
  await expect(page.getByTestId('evaluation-metrics')).toContainText(`${report.workflow.passed} / ${report.workflow.total}`);
  await expect(page.locator('.metrics-table tbody tr')).toHaveCount(methods.length);
  await expect(page.locator('.workflow-case')).toHaveCount(report.workflow.cases.length);
  await page.locator('.workflow-case summary').first().click();
  await expect(page.locator('.case-detail').first()).toContainText(report.workflow.cases[0].actual);
  await expectNoHorizontalOverflow(page);
  await saveScreenshot(page, 'evaluation.png');
});
