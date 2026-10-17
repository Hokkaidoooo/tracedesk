import { createHash, randomUUID } from 'node:crypto';
import { mkdir, readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, test } from '@playwright/test';
import type { Page, Response } from '@playwright/test';
import type { CreatedSession, DocumentEvidence } from '../src/api';
import type { KnowledgeCatalog, KnowledgeDocument, KnowledgeSnapshot, SearchResult } from '../src/knowledge-api';

const output = process.env.TRACEDESK_E2E_OUTPUT_DIR
  ?? fileURLToPath(new URL('../test-results/', import.meta.url));

function responseAt(path: string, method = 'POST'): (response: Response) => boolean {
  return (response) => new URL(response.url()).pathname === path && response.request().method() === method;
}

async function start(page: Page): Promise<CreatedSession> {
  const creation = page.waitForResponse(responseAt('/api/sessions'));
  await page.goto('/');
  const response = await creation;
  expect(response.ok()).toBe(true);
  const session = await response.json() as CreatedSession;
  await expect(page.locator('#message-input')).toBeEnabled();
  await page.locator('.rail [data-view="knowledge"]').click();
  await expect(page.locator('#knowledge-view')).toBeVisible();
  await expect(page.locator('#knowledge-demo')).toBeEnabled();
  return session;
}

async function upload(page: Page, title: string, text: string, filename = 'guide.md'): Promise<KnowledgeDocument> {
  await page.locator('#knowledge-file').setInputFiles({
    name: filename, mimeType: 'text/markdown', buffer: Buffer.from(text),
  });
  await page.locator('#knowledge-document-title').fill(title);
  const pending = page.waitForResponse(responseAt('/api/knowledge/documents'));
  await page.locator('#knowledge-upload').click();
  const response = await pending;
  expect(response.status()).toBe(201);
  const document = await response.json() as KnowledgeDocument;
  expect(document.status).toBe('ready');
  await expect(page.locator('#knowledge-refresh')).toBeEnabled();
  return document;
}

async function publish(page: Page): Promise<KnowledgeSnapshot> {
  const pending = page.waitForResponse(responseAt('/api/knowledge/snapshots'));
  await page.locator('#knowledge-build').click();
  const response = await pending;
  expect(response.status()).toBe(201);
  const snapshot = await response.json() as KnowledgeSnapshot;
  expect(snapshot.status).toBe('published');
  await expect(page.locator('#knowledge-snapshot')).toHaveValue(snapshot.id);
  await expect(page.locator('#knowledge-search')).toBeEnabled();
  return snapshot;
}

async function search(page: Page, query: string): Promise<SearchResult> {
  await page.locator('#knowledge-query').fill(query);
  const pending = page.waitForResponse(responseAt('/api/knowledge/search'));
  await page.locator('#knowledge-query').press('Enter');
  const response = await pending;
  expect(response.ok()).toBe(true);
  const result = await response.json() as SearchResult;
  await expect(page.locator('#knowledge-refresh')).toBeEnabled();
  return result;
}

async function screenshot(page: Page, name: string): Promise<void> {
  await mkdir(join(output, 'screenshots'), { recursive: true });
  await page.screenshot({ path: join(output, 'screenshots', name), fullPage: true });
}

test('真实资料上传、发布、原文检索与授权下载，历史版本保持可查', async ({ page }) => {
  test.setTimeout(90_000);
  const session = await start(page);
  const marker = 'probe' + randomUUID().replaceAll('-', '');
  const title = '原创上传验收 ' + marker;
  const text = `${marker} 资料约定：七天内支持申请退款。<img src=x onerror="window.__knowledgeScriptExecuted=true">`;
  const document = await upload(page, title, text);
  const first = await publish(page);
  await expect(page.locator('#knowledge-build-embeddings')).toBeDisabled();
  await expect(page.locator('#knowledge-method option[value="semantic"]')).toBeDisabled();
  const result = await search(page, marker);
  expect(result.snapshot_id).toBe(first.id);
  expect(result.evidence[0].document_id).toBe(document.document_id);
  expect(result.evidence[0].document_version).toBe(1);
  const citations = page.locator('#knowledge-results');
  await expect(citations).toContainText('不提供页码');
  await expect(citations).toContainText(document.source_sha256);
  await expect(citations).toContainText('<img');
  await expect(citations).not.toContainText('生效日期');
  await expect(citations.locator('img')).toHaveCount(0);
  expect(await page.evaluate(() => '__knowledgeScriptExecuted' in window)).toBe(false);

  const originalResponse = page.waitForResponse((response) =>
    new URL(response.url()).pathname.endsWith('/versions/1/source'));
  const downloadPromise = page.waitForEvent('download');
  await citations.getByRole('button', { name: '下载原件', exact: true }).first().click();
  const download = await downloadPromise;
  const sourceResponse = await originalResponse;
  expect(sourceResponse.request().headers().authorization).toBe('Bearer ' + session.token);
  expect(sourceResponse.url()).not.toContain(session.token);
  expect(download.suggestedFilename()).toBe('guide.md');
  const downloaded = await download.path();
  expect(downloaded).not.toBeNull();
  const bytes = await readFile(downloaded!);
  expect(bytes.toString()).toBe(text);
  expect(createHash('sha256').update(bytes).digest('hex')).toBe(document.source_sha256);
  await expect(page.locator('#knowledge-refresh')).toBeEnabled();

  const secondDocument = await upload(page, title, marker + ' 新版本：十四天内可申请。');
  expect(secondDocument.version).toBe(2);
  const second = await publish(page);
  expect(second.id).not.toBe(first.id);
  await page.locator('#knowledge-snapshot').selectOption(first.id);
  await expect(page.locator('#knowledge-search')).toBeEnabled();
  const historical = await search(page, marker);
  expect(historical.snapshot_id).toBe(first.id);
  expect(historical.evidence[0].document_version).toBe(1);
  await expect(page.locator('#knowledge-results')).toContainText('七天内支持申请退款');
  await screenshot(page, 'knowledge-desktop.png');
});

test('真实示例资料发布关系图，按可用能力提供检索和研究入口', async ({ page }) => {
  await start(page);
  const pending = page.waitForResponse(responseAt('/api/knowledge/demo'));
  await page.locator('#knowledge-demo').click();
  const response = await pending;
  expect(response.status()).toBe(201);
  const catalog = await response.json() as KnowledgeCatalog;
  expect(catalog.methods).toContain('graph');
  expect(catalog.current_snapshot?.status).toBe('published');
  await expect(page.locator('#knowledge-method option[value="graph"]')).toBeEnabled();
  await expect(page.locator('#knowledge-use-research')).toBeDisabled();
  await expect(page.locator('#conversation-mode option[value="research"]')).toBeDisabled();
  const relations = JSON.parse(await readFile(
    fileURLToPath(new URL('../../src/tracedesk/data/knowledge-demo-relations.json', import.meta.url)), 'utf8',
  )) as Array<{ subject: string }>;
  await page.locator('#knowledge-method').selectOption('graph');
  const result = await search(page, relations[0].subject);
  expect(result.evidence.length).toBeGreaterThan(0);
  expect(result.evidence.some((source) => source.graph_path.length > 0)).toBe(true);
  await expect(page.locator('#knowledge-results')).toContainText('关系路径');
});

test('客户切换清除查询状态，旧请求的迟到响应不能覆盖新会话', async ({ page }) => {
  const original = await start(page);
  const marker = 'scope' + randomUUID().replaceAll('-', '');
  await upload(page, marker, marker + ' 仅用于验证会话查询状态。');
  await publish(page);
  await search(page, marker);
  let release: () => void = () => { throw new Error('尚未建立延迟响应'); };
  const held = new Promise<void>((resolve) => { release = resolve; });
  let captured: () => void = () => { throw new Error('尚未捕获旧响应'); };
  const oldCaptured = new Promise<void>((resolve) => { captured = resolve; });
  let hold = true;
  await page.route('**/api/knowledge', async (route) => {
    if (!hold) { await route.continue(); return; }
    hold = false;
    const response = await route.fetch();
    captured();
    await held;
    await route.fulfill({ response });
  });
  await page.locator('#knowledge-refresh').click();
  await oldCaptured;
  const nextCreation = page.waitForResponse(responseAt('/api/sessions'));
  const nextCatalog = page.waitForResponse((response) =>
    new URL(response.url()).pathname === '/api/knowledge'
    && response.request().headers().authorization !== 'Bearer ' + original.token);
  await page.locator('#knowledge-customer').selectOption('customer-b');
  const next = await (await nextCreation).json() as CreatedSession;
  expect(next.customer.id).toBe('customer-b');
  expect((await nextCatalog).request().headers().authorization).toBe('Bearer ' + next.token);
  release();
  await expect(page.locator('#knowledge-demo')).toBeEnabled();
  await expect(page.locator('#knowledge-customer')).toHaveValue('customer-b');
  await expect(page.locator('#knowledge-query')).toHaveValue('');
  await expect(page.locator('#knowledge-results .citation-card')).toHaveCount(0);
  await expect(page.locator('#customer-select')).toHaveValue('customer-b');
  // 两位演示客户属于同一商店，资料可共享；查询结果和会话身份必须重新读取。
});

test('390 像素资料库可用键盘检索且没有横向溢出', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await start(page);
  const marker = 'mobile' + randomUUID().replaceAll('-', '');
  await upload(page, '手机资料验收', marker + ' 保修期限为十二个月。');
  await publish(page);
  await page.locator('#knowledge-query').scrollIntoViewIfNeeded();
  await search(page, marker);
  await expect(page.locator('#knowledge-results')).toContainText('十二个月');
  const widths = await page.evaluate(() => ({
    viewport: innerWidth, body: document.body.scrollWidth, root: document.documentElement.scrollWidth,
    height: innerHeight, documentHeight: document.documentElement.scrollHeight,
    hiddenLabels: [...document.querySelectorAll<HTMLElement>('.sr-only')].map((node) => ({
      text: node.textContent, top: node.getBoundingClientRect().top,
      position: getComputedStyle(node).position, parent: node.parentElement?.className,
    })),
  }));
  expect(widths.body).toBeLessThanOrEqual(widths.viewport);
  expect(widths.root).toBeLessThanOrEqual(widths.viewport);
  expect(widths.documentHeight, JSON.stringify(widths)).toBeLessThanOrEqual(widths.height);
  await screenshot(page, 'knowledge-mobile.png');
});

test('研究 HTTP 协议夹具：重试固定模式与快照，变更版本创建新请求', async ({ page }) => {
  test.setTimeout(90_000);
  // 仅模拟尚未配置的模型响应；资料、发布和来源仍来自真实后端。
  await page.route('**/api/knowledge', async (route) => {
    const response = await route.fetch();
    const data = await response.json() as KnowledgeCatalog;
    data.capabilities.research = true;
    data.capabilities.research_default_mode = 'single';
    await route.fulfill({ response, json: data });
  });
  const session = await start(page);
  const marker = 'research' + randomUUID().replaceAll('-', '');
  await upload(page, '研究协议夹具原文', marker + ' 这份资料用于验证引用呈现和请求上下文。');
  const first = await publish(page);
  const second = await publish(page);
  const sourceResponse = await page.request.post('/api/knowledge/search', {
    headers: { Authorization: 'Bearer ' + session.token },
    data: { query: marker, method: 'lexical', snapshot_id: second.id, limit: 1 },
  });
  expect(sourceResponse.ok()).toBe(true);
  const source = ((await sourceResponse.json()) as SearchResult).evidence[0];
  expect(source).toBeDefined();
  const requests: Array<{ message: string; request_id: string; mode: 'multi' | 'single'; snapshot_id: string }> = [];
  await page.route('**/api/sessions/*/research', async (route) => {
    const body = route.request().postDataJSON() as typeof requests[number];
    requests.push(body);
    if (requests.length === 1 || requests.length === 3) {
      await route.abort('failed');
      return;
    }
    const evidence: DocumentEvidence = { ...source, snapshot_id: body.snapshot_id, claim_id: 'fixture-claim-1' };
    const insufficient = requests.length === 4;
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        id: 'fixture-turn-' + body.request_id, run_id: 'fixture-run-' + body.request_id,
        request_id: body.request_id, message: body.message,
        answer: insufficient ? '协议夹具：现有证据不足。' : '协议夹具：这一条结论有原文依据。',
        status: insufficient ? 'insufficient_evidence' : 'partially_supported',
        runtime: `research:${body.mode}:${body.snapshot_id}`,
        citations: insufficient ? [] : [evidence], order: null, action: null,
        created_at: new Date().toISOString(),
        trace: [{ seq: 1, stage: 'research.verification', label: '逐项证据核验', status: 'partial', detail: 'HTTP 协议夹具，未调用真实模型。', duration_ms: 0.5 }],
      }),
    });
  });
  await page.locator('#knowledge-use-research').click();
  await expect(page.locator('#conversation-mode')).toHaveValue('research');
  await expect(page.locator('#research-mode')).toHaveValue('single');
  await page.locator('#research-mode').selectOption('multi');
  await page.locator('.rail [data-view="knowledge"]').click();
  await page.locator('#knowledge-refresh').click();
  await expect(page.locator('#knowledge-refresh')).toBeEnabled();
  await page.locator('.rail [data-view="workspace"]').click();
  await expect(page.locator('#research-mode')).toHaveValue('multi');
  await expect(page.locator('#research-snapshot')).toHaveValue(second.id);
  await page.locator('#message-input').fill('请根据资料核对这条结论。');
  await page.locator('#message-input').press('Enter');
  await expect(page.locator('#message-feedback')).toContainText('NETWORK_ERROR');
  await page.getByRole('button', { name: '重试发送', exact: true }).click();
  await expect(page.getByTestId('assistant-message')).toHaveCount(1);
  expect(requests[1]).toEqual(requests[0]);
  expect(requests[1].mode).toBe('multi');
  await expect(page.getByTestId('assistant-message')).toContainText('部分结论有依据');
  await expect(page.locator('#evidence-content')).toContainText('fixture-claim-1');
  await expect(page.locator('#evidence-content')).not.toContainText('生效日期');
  await page.locator('#research-mode').selectOption('single');
  await page.locator('#message-input').fill('请根据资料核对这条结论。');
  await page.locator('#message-input').press('Enter');
  await expect(page.locator('#message-feedback')).toContainText('NETWORK_ERROR');
  await page.locator('#research-snapshot').selectOption(first.id);
  await page.locator('#message-input').press('Enter');
  await expect(page.getByTestId('assistant-message')).toHaveCount(2);
  expect(requests[3].message).toBe(requests[2].message);
  expect(requests[3].mode).toBe('single');
  expect(requests[3].snapshot_id).toBe(first.id);
  expect(requests[3].request_id).not.toBe(requests[2].request_id);
  await expect(page.getByTestId('assistant-message').last()).toContainText('证据不足');
  await expect(page.getByTestId('assistant-message').last()).toContainText('单角色研究');
});

test('真实解析失败保留原件，显式重试仍显示失败，新的有效版本可发布', async ({ page }) => {
  const session = await start(page);
  const title = '失败重试验收 ' + randomUUID();
  await page.locator('#knowledge-file').setInputFiles({
    name: 'invalid.txt', mimeType: 'text/plain', buffer: Buffer.from([0xff, 0xfe, 0x81]),
  });
  await page.locator('#knowledge-document-title').fill(title);
  const uploadResponse = page.waitForResponse(responseAt('/api/knowledge/documents'));
  await page.locator('#knowledge-upload').click();
  expect((await uploadResponse).status()).toBe(422);
  const row = page.getByTestId('knowledge-document').filter({ hasText: title });
  await expect(row).toContainText('DOCUMENT_ENCODING');
  await expect(row.getByRole('button', { name: '重试解析', exact: true })).toBeEnabled();
  const id = await row.getAttribute('data-document-id');
  const retry = page.waitForResponse(responseAt(`/api/knowledge/documents/${id}/versions/1/retry`));
  await row.getByRole('button', { name: '重试解析', exact: true }).click();
  expect((await retry).status()).toBe(422);
  await expect(row).toContainText('解析失败');
  const original = await page.request.get(`/api/knowledge/documents/${id}/versions/1/source`, {
    headers: { Authorization: 'Bearer ' + session.token },
  });
  expect(original.ok()).toBe(true);
  expect(await original.body()).toEqual(Buffer.from([0xff, 0xfe, 0x81]));
  await expect(page.locator('#knowledge-refresh')).toBeEnabled();
  const repaired = await upload(page, title, '这是有效的第二版 UTF-8 资料。', 'valid.md');
  expect(repaired.version).toBe(2);
  await publish(page);
  await expect(page.getByTestId('knowledge-document').filter({ hasText: title })).toHaveCount(2);
});
