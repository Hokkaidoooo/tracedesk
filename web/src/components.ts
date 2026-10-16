import { ApiError } from './api';
import type { ActionState, Citation, EvaluationReport, Order, Receipt, RefundAction, TraceEvent, Turn, WorkflowCase } from './api';
import { button, definition, element, money, runtimeLabel, timestamp } from './dom';

export interface ActionView {
  requestId?: string;
  state?: ActionState;
  receipt?: Receipt;
  error?: ApiError;
  processing?: 'confirm' | 'cancel';
}

interface TurnContext {
  selected: boolean;
  busy: boolean;
  actionView?: ActionView;
  inspect: (tab: 'evidence' | 'trace', citationIndex?: number) => void;
  act: (operation: 'confirm' | 'cancel') => void;
}

const turnStatusLabels: Record<Turn['status'], string> = {
  answered: '已回复', needs_confirmation: '待确认', needs_information: '待补充信息', handoff: '建议人工处理',
};

const orderStatusLabels: Record<string, string> = {
  delivered: '已签收', shipped: '已发货', paid: '已支付', pending: '待处理',
  processing: '处理中', refund_requested: '已申请退款', refunded: '已退款', cancelled: '已取消',
};

export function errorBlock(error: ApiError, retry?: () => void, retryLabel = '重试'): HTMLDivElement {
  const block = element('div', 'error-block');
  const text = element('div', 'error-copy');
  text.append(element('strong', 'error-code', error.code), element('p', undefined, error.message));
  block.append(text);
  if (retry) block.append(button(retryLabel, 'text-button', retry));
  return block;
}

function renderOrder(order: Order): HTMLElement {
  const card = element('section', 'order-card');
  card.setAttribute('aria-label', `订单 ${order.id}`);
  card.dataset.testid = 'order-card';
  const heading = element('div', 'card-heading');
  heading.append(element('span', 'monospace', order.id), element('span', 'neutral-pill', orderStatusLabels[order.status] ?? order.status));
  const details = element('div', 'order-summary');
  const product = element('div');
  product.append(element('strong', undefined, order.product));
  if (order.delivered_at) product.append(element('p', undefined, `签收于 ${timestamp(order.delivered_at)}`));
  details.append(product, element('strong', 'order-amount', money(order.amount_minor, order.currency)));
  card.append(heading, details);
  return card;
}

function appendReceipt(card: HTMLElement, receipt: Pick<Receipt, 'receipt_id' | 'submitted_at' | 'replayed'>): void {
  const list = element('dl', 'receipt-details');
  list.dataset.testid = 'refund-receipt';
  list.append(definition('申请回执', receipt.receipt_id), definition('提交时间', timestamp(receipt.submitted_at)));
  card.append(list);
  if (receipt.replayed) card.append(element('p', 'card-hint', '此次重试返回了同一份申请回执。'));
}

function renderAction(action: RefundAction, context: TurnContext): HTMLElement {
  const view = context.actionView;
  const actionState = view?.state ?? action.state;
  const card = element('section', `refund-card state-${actionState}`);
  card.setAttribute('aria-label', '售后申请');
  card.setAttribute('aria-busy', String(Boolean(view?.processing)));
  card.dataset.actionId = action.id;
  card.dataset.state = actionState;
  card.dataset.testid = 'refund-card';
  card.tabIndex = -1;
  const titles: Record<ActionState, string> = {
    pending: '请确认售后申请', submitted: '售后申请已提交', cancelled: '售后申请已取消', expired: '售后申请已过期',
  };
  const heading = element('div', 'card-heading');
  heading.append(element('h3', undefined, titles[actionState]), element('span', 'action-pill', actionState === 'pending' ? '待你确认' : titles[actionState].replace('售后申请', '')));
  const list = element('dl', 'action-details');
  list.append(definition('订单', action.order_id), definition('申请金额', money(action.amount_minor, action.currency)), definition('申请原因', action.reason));
  card.append(heading, list);

  if (view?.receipt) appendReceipt(card, view.receipt);
  else if (actionState === 'submitted' && action.receipt_id && action.submitted_at) {
    appendReceipt(card, { receipt_id: action.receipt_id, submitted_at: action.submitted_at, replayed: false });
  }

  if (actionState === 'pending') {
    card.append(element('p', 'action-expiration', `确认有效期至 ${timestamp(action.expires_at)}`));
    const controls = element('div', 'action-controls');
    const confirm = button(view?.processing === 'confirm' ? '正在提交…' : '确认提交申请', 'button-primary', () => context.act('confirm'));
    const cancel = button(view?.processing === 'cancel' ? '正在取消…' : '取消申请', 'button-secondary', () => context.act('cancel'));
    confirm.disabled = context.busy;
    cancel.disabled = context.busy;
    controls.append(confirm, cancel);
    card.append(controls);
  }
  if (actionState === 'expired') card.append(element('p', 'card-hint', '请重新发送退货申请，核对最新订单信息。'));
  if (actionState === 'submitted') card.append(element('p', 'card-hint', '申请已记入演示系统，不会产生真实退款。'));
  if (actionState === 'cancelled') card.append(element('p', 'card-hint', '本次申请未提交。需要时可以重新申请。'));
  if (view?.error) card.append(errorBlock(view.error));
  return card;
}

export function renderTurn(turn: Turn, context: TurnContext): HTMLElement {
  const section = element('section', 'turn');
  section.dataset.turnId = turn.id;
  const user = element('div', 'user-message');
  const userMeta = element('div', 'message-meta');
  userMeta.append(element('span', undefined, '你'), element('time', undefined, timestamp(turn.created_at, true)));
  user.append(userMeta, element('p', 'user-bubble', turn.message));

  const assistant = element('article', `assistant-message${context.selected ? ' selected' : ''}`);
  assistant.dataset.turnId = turn.id;
  assistant.dataset.testid = 'assistant-message';
  const header = element('div', 'assistant-heading');
  header.append(element('span', 'assistant-avatar', 'T'), element('strong', undefined, 'TraceDesk'), element('span', 'answer-mode', runtimeLabel(turn.runtime)));
  if (turn.status !== 'answered') header.append(element('span', `turn-status status-${turn.status}`, turnStatusLabels[turn.status]));
  const answer = element('p', 'answer-text', turn.answer);
  answer.dataset.testid = 'answer-text';
  assistant.append(header, answer);

  if (turn.citations.length) {
    const references = element('div', 'reference-links');
    turn.citations.forEach((citation, index) => {
      const reference = button(`${index + 1} · ${citation.title}`, 'reference-button', () => context.inspect('evidence', index));
      reference.setAttribute('aria-label', `查看引用 ${index + 1}：${citation.title}，版本 ${citation.version}`);
      references.append(reference);
    });
    assistant.append(references);
  }
  if (turn.order) assistant.append(renderOrder(turn.order));
  if (turn.action) assistant.append(renderAction(turn.action, context));
  const footer = element('div', 'turn-footer');
  const inspect = button('查看依据与记录 ↗', 'inspect-turn text-button', () => context.inspect('evidence'));
  inspect.setAttribute('aria-pressed', String(context.selected));
  footer.append(inspect, element('span', undefined, `${turn.trace.length} 条执行记录`));
  assistant.append(footer);
  section.append(user, assistant);
  return section;
}

export function renderCitations(citations: Citation[]): DocumentFragment {
  const fragment = document.createDocumentFragment();
  if (!citations.length) {
    const empty = element('div', 'inspector-empty');
    empty.append(element('div', 'empty-evidence-icon', '§'), element('h3', undefined, '本次没有政策引用'), element('p', undefined, '可切换到执行记录，查看此次请求实际完成的操作。'));
    fragment.append(empty);
    return fragment;
  }
  fragment.append(element('p', 'inspector-description', '展开引用，核对回答所依据的原文。'));
  citations.forEach((citation, index) => {
    const details = element('details', 'citation-card');
    details.dataset.citationIndex = String(index);
    details.open = index === 0;
    const summary = element('summary');
    summary.append(element('span', 'citation-index', String(index + 1)), element('strong', undefined, citation.title), element('span', 'citation-version', `v${citation.version}`));
    const body = element('div', 'citation-body');
    const quote = element('blockquote', 'citation-quote', citation.quote);
    quote.dataset.testid = 'citation-quote';
    body.append(quote);
    const metadata = element('dl', 'citation-metadata');
    metadata.append(definition('来源', citation.source_id), definition('生效日期', citation.effective_from), definition('失效日期', citation.effective_to ?? '未设定'));
    metadata.append(citation.score === null
      ? definition('依据来源', '按政策版本直接读取')
      : definition('检索分值', citation.score.toFixed(3)));
    body.append(metadata);
    details.append(summary, body);
    fragment.append(details);
  });
  if (citations.some((citation) => citation.score !== null)) {
    fragment.append(element('p', 'retrieval-note', '检索分值用于排列引用，不代表回答正确率。'));
  }
  return fragment;
}

export function renderTrace(trace: TraceEvent[], runId: string): DocumentFragment {
  const fragment = document.createDocumentFragment();
  if (!trace.length) {
    const empty = element('div', 'inspector-empty');
    empty.append(element('h3', undefined, '没有执行记录'), element('p', undefined, '该回答未返回执行事件。'));
    fragment.append(empty);
    return fragment;
  }
  const list = element('ol', 'trace-list');
  trace.forEach((event) => {
    const row = element('li', `trace-event trace-${event.status}`);
    row.dataset.testid = 'trace-event';
    row.append(element('span', 'trace-marker', String(event.seq)));
    const body = element('div', 'trace-body');
    const heading = element('div', 'trace-heading');
    heading.append(element('strong', undefined, event.label), element('span', 'trace-time', `${event.duration_ms.toFixed(1)} ms`));
    const meta = element('div', 'trace-meta');
    const labels: Record<string, string> = { ok: '完成', denied: '已拒绝', error: '失败', skipped: '已跳过', blocked: '已阻止' };
    meta.append(element('span', 'monospace', event.stage), element('span', 'trace-state', labels[event.status] ?? event.status));
    body.append(heading, element('p', undefined, event.detail), meta);
    row.append(body);
    list.append(row);
  });
  const run = element('div', 'run-reference');
  run.append(element('span', 'eyebrow', '运行编号'), element('code', undefined, runId));
  fragment.append(element('p', 'inspector-description', '按发生顺序列出本次请求的可观察事件。'), list, run);
  return fragment;
}

function metricCard(label: string, value: string, detail: string): HTMLElement {
  const card = element('section', 'metric-card');
  card.append(element('h2', undefined, label), element('strong', 'metric-value', value), element('p', undefined, detail));
  return card;
}

function renderWorkflowCases(cases: WorkflowCase[]): HTMLElement {
  const list = element('div', 'workflow-cases');
  for (const entry of cases) {
    const details = element('details', 'workflow-case');
    const summary = element('summary');
    summary.append(element('strong', undefined, entry.name), element('span', entry.passed ? 'pass-badge' : 'fail-badge', entry.passed ? '通过' : '未通过'));
    const body = element('dl', 'case-detail');
    body.append(definition('用例编号', entry.id), definition('预期结果', entry.expected), definition('实际结果', entry.actual));
    details.append(summary, body);
    list.append(details);
  }
  return list;
}

export function renderEvaluation(report: EvaluationReport): DocumentFragment {
  const fragment = document.createDocumentFragment();
  const scope = element('section', 'evaluation-scope');
  scope.append(element('h2', undefined, '结果适用范围'), element('p', undefined, report.scope));
  const metadata = element('div', 'report-metadata');
  metadata.append(element('span', undefined, `生成时间 ${new Date(report.generated_at).toLocaleString('zh-CN', { hour12: false })}`));
  const hash = element('details', 'dataset-hash');
  hash.append(element('summary', undefined, '查看数据集 SHA-256'), element('code', undefined, report.dataset_sha256));
  metadata.append(hash);

  const cards = element('div', 'metrics-grid');
  cards.dataset.testid = 'evaluation-metrics';
  cards.append(metricCard('工作流通过', `${report.workflow.passed} / ${report.workflow.total}`, '按报告中的实际执行结果统计'));
  const methods = Object.entries(report.retrieval);
  for (const [name, metrics] of methods) {
    cards.append(metricCard(`${name.toUpperCase()} · Recall@3`, `${(metrics.recall_at_3 * 100).toFixed(1)}%`, `${metrics.queries} 条检索问题 · ${metrics.answerable} 条可回答`));
  }

  const retrievalSection = element('section', 'evaluation-section');
  retrievalSection.append(element('h2', undefined, '检索方法对比'), element('p', 'section-description', 'Recall@3 为前三条结果的命中比例；MRR 反映首条相关结果的排名。'));
  const wrapper = element('div', 'table-wrapper');
  wrapper.tabIndex = 0;
  wrapper.setAttribute('aria-label', '检索评测指标表，可横向滚动');
  const table = element('table', 'metrics-table');
  const caption = element('caption', 'sr-only', '固定合成回归集上的检索指标');
  const thead = element('thead');
  const headerRow = element('tr');
  for (const title of ['方法', 'Recall@3', 'MRR', '无答案识别率', '检索中位耗时']) {
    const cell = element('th', undefined, title);
    cell.scope = 'col';
    headerRow.append(cell);
  }
  thead.append(headerRow);
  const tbody = element('tbody');
  for (const [name, metrics] of methods) {
    const row = element('tr');
    const method = element('th', 'monospace', name);
    method.scope = 'row';
    row.append(method);
    for (const value of [`${(metrics.recall_at_3 * 100).toFixed(1)}%`, metrics.mrr.toFixed(3), `${(metrics.no_answer_accuracy * 100).toFixed(1)}%`, `${metrics.latency_ms_p50.toFixed(2)} ms`]) row.append(element('td', undefined, value));
    tbody.append(row);
  }
  table.append(caption, thead, tbody);
  wrapper.append(table);
  retrievalSection.append(wrapper);

  const workflow = element('section', 'evaluation-section');
  workflow.append(element('h2', undefined, '工作流用例'), element('p', 'section-description', '展开查看用例的实际断言结果。'), renderWorkflowCases(report.workflow.cases));
  fragment.append(scope, metadata, cards, retrievalSection, workflow);
  return fragment;
}
