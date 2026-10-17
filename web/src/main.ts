import './style.css';
import { api, ApiError, asApiError } from './api';
import type { AppInfo, CreatedSession, DemoCustomer, EvaluationReport, ResearchMode, Turn } from './api';
import { errorBlock, renderCitations, renderEvaluation, renderTrace, renderTurn } from './components';
import type { ActionView } from './components';
import { button, element, required, runtimeLabel } from './dom';
import type { KnowledgeCatalog } from './knowledge-api';
import { KnowledgeView, snapshotLabel } from './knowledge-view';

type PendingRequest = { message: string; requestId: string } & (
  { kind: 'support' } | { kind: 'research'; mode: ResearchMode; snapshotId: string }
);

interface SessionView extends CreatedSession {
  turns: Turn[];
  draft: string;
  taskMode: 'support' | 'research';
  researchMode: ResearchMode;
  researchModeSelected: boolean;
  snapshotId: string | null;
  pendingRequest?: PendingRequest;
  sendError?: ApiError;
}

type InspectorTab = 'evidence' | 'trace';
type Panel = 'conversation' | 'sessions' | 'evidence';

const dom = {
  app: required('#app'),
  customerSelect: required<HTMLSelectElement>('#customer-select'),
  newSession: required<HTMLButtonElement>('#new-session'),
  sessionList: required('#session-list'),
  log: required('#conversation-log'),
  input: required<HTMLTextAreaElement>('#message-input'),
  send: required<HTMLButtonElement>('#send-message'),
  form: required<HTMLFormElement>('#message-form'),
  globalFeedback: required('#global-feedback'),
  messageFeedback: required('#message-feedback'),
  evidence: required('#evidence-content'),
  trace: required('#trace-content'),
  evaluation: required('#evaluation-content'),
  refreshEvaluation: required<HTMLButtonElement>('#refresh-evaluation'),
  taskMode: required<HTMLSelectElement>('#conversation-mode'),
  researchMode: required<HTMLSelectElement>('#research-mode'),
  researchSnapshot: required<HTMLSelectElement>('#research-snapshot'),
};

const welcome = dom.log.firstElementChild?.cloneNode(true);
if (!welcome) throw new Error('缺少初始会话内容。');
const initialEvidence = dom.evidence.firstElementChild?.cloneNode(true);
if (!initialEvidence) throw new Error('缺少初始引用内容。');

const state = {
  info: null as AppInfo | null,
  customers: [] as DemoCustomer[],
  sessions: [] as SessionView[],
  activeId: null as string | null,
  selectedTurnId: null as string | null,
  busy: null as string | null,
  ready: false,
  actions: new Map<string, ActionView>(),
  evaluation: null as EvaluationReport | null,
  evaluationLoading: false,
  evaluationAttempted: false,
  catalog: null as KnowledgeCatalog | null,
};

function activeSession(): SessionView | undefined {
  return state.sessions.find((session) => session.id === state.activeId);
}

function announce(message: string): void {
  required('#announcement').textContent = message;
}

function showGlobalError(error: unknown, retry?: () => void): void {
  dom.globalFeedback.replaceChildren(errorBlock(asApiError(error), retry));
  dom.globalFeedback.hidden = false;
}

function clearGlobalError(): void {
  dom.globalFeedback.hidden = true;
  dom.globalFeedback.replaceChildren();
}

function updateControls(): void {
  const session = activeSession();
  const disabled = state.busy !== null;
  dom.customerSelect.disabled = disabled || !state.ready;
  dom.newSession.disabled = disabled || !state.ready;
  dom.input.disabled = !session;
  dom.input.readOnly = disabled;
  renderResearchControls();
  const researchUnavailable = session?.taskMode === 'research'
    && (!state.catalog?.capabilities.research || !session.snapshotId);
  dom.send.disabled = disabled || !session || !dom.input.value.trim() || researchUnavailable;
  dom.send.setAttribute('aria-label', disabled ? state.busy ?? '处理中' : '发送消息');
  dom.form.setAttribute('aria-busy', String(disabled));
  required('#character-count').textContent = `${dom.input.value.length} / 2000`;
  dom.sessionList.querySelectorAll<HTMLButtonElement>('button').forEach((node) => { node.disabled = disabled; });
  dom.log.querySelectorAll<HTMLButtonElement>('.action-controls button').forEach((node) => { node.disabled = disabled; });
  document.querySelectorAll<HTMLButtonElement>('[data-message]').forEach((node) => { node.disabled = disabled || !session; });
  const mode = state.info ? runtimeLabel(state.info.runtime) : '正在连接服务';
  required('#composer-caption').textContent = disabled
    ? `${state.busy}…`
    : session?.taskMode === 'research'
      ? '研究只依据所选资料版本作答，请结合引用核对结论。'
      : `${mode} · 演示售后申请不会产生真实退款。`;
  knowledge.setIdentityBusy(disabled || !state.ready);
}

function renderResearchControls(): void {
  const session = activeSession();
  const published = state.catalog?.snapshots.filter((item) => item.status === 'published') ?? [];
  const enabled = Boolean(state.catalog?.capabilities.research && published.length);
  dom.taskMode.disabled = state.busy !== null || !session;
  required<HTMLOptionElement>('#conversation-mode option[value="research"]').disabled = !enabled;
  dom.taskMode.value = session?.taskMode ?? 'support';
  required('#research-options').hidden = session?.taskMode !== 'research';
  dom.researchMode.disabled = state.busy !== null || !enabled;
  dom.researchMode.value = session?.researchMode ?? 'multi';
  dom.researchSnapshot.replaceChildren(...published.map((snapshot) => {
    const option = element('option', undefined, snapshotLabel(snapshot, state.catalog?.current_snapshot?.id));
    option.value = snapshot.id;
    return option;
  }));
  if (!published.length) dom.researchSnapshot.append(element('option', undefined, '尚无已发布资料'));
  if (session && !session.snapshotId && state.catalog?.current_snapshot) {
    session.snapshotId = state.catalog.current_snapshot.id;
  }
  dom.researchSnapshot.value = session?.snapshotId ?? '';
  dom.researchSnapshot.disabled = state.busy !== null || !enabled;
  required('#research-availability').textContent = !state.catalog
    ? '正在读取资料研究状态。'
    : !state.catalog.capabilities.research
      ? '资料研究尚未启用，资料库仍可检索已发布的原文。'
      : !published.length
        ? '先在资料库发布资料，再开始研究。'
        : '协作研究和单角色研究都会核验引用证据。';
}

function setBusy(label: string | null): void {
  state.busy = label;
  updateControls();
}

function selectPanel(panel: Panel): void {
  dom.app.dataset.panel = panel;
  document.querySelectorAll<HTMLButtonElement>('[data-panel]').forEach((node) => {
    node.setAttribute('aria-pressed', String(node.dataset.panel === panel));
  });
}

function setInspector(tab: InspectorTab, focus = false): void {
  document.querySelectorAll<HTMLButtonElement>('[data-inspector]').forEach((node) => {
    const selected = node.dataset.inspector === tab;
    node.setAttribute('aria-selected', String(selected));
    node.tabIndex = selected ? 0 : -1;
    if (selected && focus) node.focus();
  });
  dom.evidence.hidden = tab !== 'evidence';
  dom.trace.hidden = tab !== 'trace';
}

function renderInspector(): void {
  const session = activeSession();
  const turn = session?.turns.find((entry) => entry.id === state.selectedTurnId);
  required('#citation-count').textContent = String(turn?.citations.length ?? 0);
  required('#trace-count').textContent = String(turn?.trace.length ?? 0);
  required('#inspector-count').textContent = turn && session ? `${session.turns.indexOf(turn) + 1} / ${session.turns.length}` : '—';
  if (!turn) {
    dom.evidence.replaceChildren(initialEvidence!.cloneNode(true));
    const empty = element('div', 'inspector-empty');
    empty.append(element('h3', undefined, '等待第一条执行记录'), element('p', undefined, '发送问题后，可以在这里查看处理过程。'));
    dom.trace.replaceChildren(empty);
    return;
  }
  dom.evidence.replaceChildren(renderCitations(turn.citations, (source) => { void knowledge.downloadEvidence(source); }));
  dom.trace.replaceChildren(renderTrace(turn.trace, turn.run_id));
}

function inspectTurn(turnId: string, tab: InspectorTab, citationIndex?: number): void {
  state.selectedTurnId = turnId;
  dom.log.querySelectorAll<HTMLElement>('.assistant-message').forEach((node) => {
    const selected = node.dataset.turnId === turnId;
    node.classList.toggle('selected', selected);
    node.querySelector('.inspect-turn')?.setAttribute('aria-pressed', String(selected));
  });
  renderInspector();
  setInspector(tab);
  selectPanel('evidence');
  if (citationIndex !== undefined) {
    const citation = dom.evidence.querySelector<HTMLDetailsElement>(`[data-citation-index="${citationIndex}"]`);
    if (citation) {
      citation.open = true;
      citation.querySelector('summary')?.focus();
      citation.scrollIntoView({ block: 'nearest' });
    }
  }
}

function renderSessionList(): void {
  const fragment = document.createDocumentFragment();
  const sessions = state.sessions.filter((session) => session.customer.id === dom.customerSelect.value);
  sessions.slice().reverse().forEach((session) => {
    const node = button('', `session-button${session.id === state.activeId ? ' active' : ''}`, () => { void openSession(session.id); });
    node.disabled = state.busy !== null;
    if (session.id === state.activeId) node.setAttribute('aria-current', 'true');
    const text = element('span', 'session-copy');
    text.append(element('strong', undefined, session.turns[0]?.message ?? '新的客服会话'), element('small', undefined, `${session.customer.name} · ${session.turns.length} 次咨询`));
    node.append(element('span', 'session-symbol', '⌑'), text);
    fragment.append(node);
  });
  if (!sessions.length) fragment.append(element('p', 'sidebar-empty', '连接后可开始会话'));
  dom.sessionList.replaceChildren(fragment);
  required('#session-count').textContent = String(sessions.length);
}

function renderMessageFeedback(): void {
  const error = activeSession()?.sendError;
  dom.messageFeedback.hidden = !error;
  dom.messageFeedback.replaceChildren();
  if (error) {
    dom.messageFeedback.append(errorBlock(error, () => { void sendMessage(); }, '重试发送'));
    dom.messageFeedback.append(element('p', 'retry-hint', '重试保留原问题、研究方式、资料版本与请求编号。修改任一项后，将发送新请求。'));
  }
}

function renderConversation(scrollToEnd = false): void {
  const session = activeSession();
  const previousScroll = dom.log.scrollTop;
  const fragment = document.createDocumentFragment();
  if (!session?.turns.length) fragment.append(welcome!.cloneNode(true));
  else session.turns.forEach((turn) => {
    fragment.append(renderTurn(turn, {
      selected: turn.id === state.selectedTurnId,
      busy: state.busy !== null,
      actionView: turn.action ? state.actions.get(turn.action.id) : undefined,
      inspect: (tab, index) => inspectTurn(turn.id, tab, index),
      act: (operation) => { void actOnRefund(session.id, turn, operation); },
    }));
  });
  dom.log.replaceChildren(fragment);
  bindScenarios(dom.log);
  dom.log.scrollTop = scrollToEnd ? dom.log.scrollHeight : previousScroll;
  required('#conversation-title').textContent = session ? `${session.customer.name}的客服会话` : '新的客服会话';
  required('#conversation-subtitle').textContent = session ? `演示客户 · ${session.customer.id}` : '选择演示身份，开始咨询';
  required('#customer-avatar').textContent = session?.customer.name.slice(0, 1) ?? 'T';
  required('#session-marker').textContent = session ? `# ${session.id.slice(0, 8)}` : 'SESSION';
  dom.input.value = session?.draft ?? '';
  renderMessageFeedback();
  updateControls();
}

function renderAll(scrollToEnd = false): void {
  renderSessionList();
  renderConversation(scrollToEnd);
  renderInspector();
}

function showCustomerOrders(): void {
  const customer = state.customers.find((entry) => entry.id === dom.customerSelect.value);
  required('#customer-orders').textContent = customer ? `可查订单：${customer.order_ids.join('、') || '暂无订单'}` : '客户和订单由演示系统提供';
}

async function createSession(customerId: string): Promise<void> {
  if (state.busy) return;
  const previous = activeSession();
  const previousTurn = state.selectedTurnId;
  setBusy('正在新建会话');
  clearGlobalError();
  state.activeId = null;
  state.selectedTurnId = null;
  dom.customerSelect.value = customerId;
  knowledge.setSession(null, state.customers, customerId);
  renderAll();
  try {
    const created = await api.createSession(customerId);
    state.sessions.push({ ...created, turns: [], draft: '', taskMode: 'support', researchMode: 'multi', researchModeSelected: false, snapshotId: null });
    state.activeId = created.id;
    state.selectedTurnId = null;
    dom.customerSelect.value = created.customer.id;
    knowledge.setSession(created, state.customers, created.customer.id);
    showCustomerOrders();
    selectPanel('conversation');
    setInspector('evidence');
    renderAll();
    announce(`已为${created.customer.name}创建会话。`);
  } catch (error) {
    state.activeId = previous?.id ?? null;
    state.selectedTurnId = previousTurn;
    if (previous) dom.customerSelect.value = previous.customer.id;
    knowledge.setSession(previous ?? null, state.customers, dom.customerSelect.value);
    showCustomerOrders();
    renderAll();
    showGlobalError(error, () => { void createSession(customerId); });
  } finally {
    setBusy(null);
  }
}

async function openSession(id: string): Promise<void> {
  if (state.busy) return;
  const session = state.sessions.find((entry) => entry.id === id);
  if (!session) return;
  setBusy('正在读取会话');
  clearGlobalError();
  try {
    const loaded = await api.session(session.id, session.token);
    session.turns = loaded.turns;
    session.customer = loaded.customer;
    state.activeId = session.id;
    state.selectedTurnId = session.turns.at(-1)?.id ?? null;
    dom.customerSelect.value = session.customer.id;
    knowledge.setSession(session, state.customers, session.customer.id);
    showCustomerOrders();
    selectPanel('conversation');
    renderAll(true);
    announce(`已打开${session.customer.name}的会话。`);
  } catch (error) {
    showGlobalError(error, () => { void openSession(id); });
  } finally {
    setBusy(null);
    renderConversation();
  }
}

async function sendMessage(): Promise<void> {
  const session = activeSession();
  if (!session || state.busy) return;
  const message = dom.input.value.trim();
  if (!message) return;
  session.draft = dom.input.value;
  session.sendError = undefined;
  if (session.taskMode === 'research' && (!state.catalog?.capabilities.research || !session.snapshotId)) {
    session.sendError = new ApiError('RESEARCH_UNAVAILABLE', '资料研究当前不可用，请检查研究状态和已发布资料版本。');
    renderMessageFeedback();
    return;
  }
  setBusy('正在请求回复');
  renderMessageFeedback();
  try {
    const proposed: PendingRequest = session.taskMode === 'research'
      ? { kind: 'research', message, requestId: crypto.randomUUID(), mode: session.researchMode, snapshotId: session.snapshotId! }
      : { kind: 'support', message, requestId: crypto.randomUUID() };
    const previous = session.pendingRequest;
    const sameContext = previous?.kind === proposed.kind && previous.message === message
      && (previous.kind === 'support'
        || (proposed.kind === 'research' && previous.mode === proposed.mode && previous.snapshotId === proposed.snapshotId));
    const pending = sameContext ? previous : proposed;
    session.pendingRequest = pending;
    const turn = pending.kind === 'research'
      ? await api.research(session.id, session.token, pending.message, pending.requestId, pending.mode, pending.snapshotId)
      : await api.sendTurn(session.id, session.token, pending.message, pending.requestId);
    const existing = session.turns.findIndex((entry) => entry.id === turn.id);
    if (existing < 0) session.turns.push(turn);
    else session.turns[existing] = turn;
    session.draft = '';
    session.pendingRequest = undefined;
    state.selectedTurnId = turn.id;
    renderAll(true);
    announce(`TraceDesk：${turn.answer}。${turn.citations.length} 条引用，${turn.trace.length} 条执行记录。`);
  } catch (error) {
    session.sendError = asApiError(error);
    renderMessageFeedback();
    announce(`发送失败：${session.sendError.message}`);
  } finally {
    setBusy(null);
    renderConversation(!session.sendError);
    dom.input.focus();
  }
}

async function actOnRefund(sessionId: string, turn: Turn, operation: 'confirm' | 'cancel'): Promise<void> {
  const action = turn.action;
  const session = state.sessions.find((entry) => entry.id === sessionId);
  if (!action || !session || state.busy) return;
  const view = state.actions.get(action.id) ?? {};
  view.error = undefined;
  view.processing = operation;
  state.actions.set(action.id, view);
  setBusy(operation === 'confirm' ? '正在提交售后申请' : '正在取消售后申请');
  renderConversation();
  try {
    if (operation === 'confirm') {
      if (!action.confirmation_token) throw new ApiError('MISSING_CONFIRMATION', '申请缺少确认凭证，请重新准备申请。');
      view.requestId ??= crypto.randomUUID();
      const receipt = await api.confirm(action.id, session.token, action.confirmation_token, view.requestId);
      view.receipt = receipt;
      view.state = receipt.state;
      announce(`售后申请已提交。回执：${receipt.receipt_id}。`);
    } else {
      const result = await api.cancel(action.id, session.token);
      view.state = result.state;
      announce('售后申请已取消。');
    }
  } catch (error) {
    view.error = asApiError(error);
    if (view.error.code === 'ACTION_EXPIRED') view.state = 'expired';
    announce(`操作失败：${view.error.message}`);
  } finally {
    view.processing = undefined;
    setBusy(null);
    renderConversation();
    dom.log.querySelector<HTMLElement>(`[data-action-id="${CSS.escape(action.id)}"]`)?.focus();
  }
}

function bindScenarios(root: ParentNode): void {
  root.querySelectorAll<HTMLButtonElement>('[data-message]').forEach((node) => {
    node.addEventListener('click', () => {
      const session = activeSession();
      if (!session || state.busy || !node.dataset.message) return;
      session.draft = node.dataset.message;
      session.taskMode = 'support';
      session.sendError = undefined;
      session.pendingRequest = undefined;
      dom.input.value = session.draft;
      selectPanel('conversation');
      renderMessageFeedback();
      updateControls();
      dom.input.focus();
    });
  });
}

async function loadEvaluation(): Promise<void> {
  if (state.evaluationLoading) return;
  state.evaluationLoading = true;
  state.evaluationAttempted = true;
  dom.refreshEvaluation.disabled = true;
  dom.refreshEvaluation.textContent = '正在读取…';
  dom.evaluation.setAttribute('aria-busy', 'true');
  try {
    const report = await api.evaluation();
    const contents = renderEvaluation(report);
    state.evaluation = report;
    dom.evaluation.replaceChildren(contents);
    announce('评测报告已读取。');
  } catch (error) {
    const failure = asApiError(error);
    const empty = element('section', 'evaluation-empty');
    empty.append(element('span', 'eyebrow', failure.status === 404 ? '暂无报告' : '读取失败'), element('h2', undefined, failure.status === 404 ? '尚未生成评测报告' : '无法读取评测报告'), errorBlock(failure));
    if (failure.status === 404) empty.append(element('p', 'section-description', '生成报告后，点击“刷新报告”查看实际结果。'));
    dom.evaluation.replaceChildren(empty);
    state.evaluation = null;
  } finally {
    state.evaluationLoading = false;
    dom.refreshEvaluation.disabled = false;
    dom.refreshEvaluation.textContent = '刷新报告';
    dom.evaluation.setAttribute('aria-busy', 'false');
  }
}

function selectView(view: string): void {
  if (view !== 'workspace' && view !== 'evaluation' && view !== 'knowledge') return;
  dom.app.dataset.view = view;
  required('#workspace-view').hidden = view !== 'workspace';
  required('#evaluation-view').hidden = view !== 'evaluation';
  required('#knowledge-view').hidden = view !== 'knowledge';
  required('#view-title').textContent = view === 'workspace' ? '客服工作台' : view === 'knowledge' ? '资料库' : '回归评测';
  document.querySelectorAll<HTMLButtonElement>('.rail-button[data-view]').forEach((node) => {
    const selected = node.dataset.view === view;
    node.classList.toggle('active', selected);
    if (selected) node.setAttribute('aria-current', 'page');
    else node.removeAttribute('aria-current');
  });
  if (view === 'evaluation' && !state.evaluationAttempted) void loadEvaluation();
}

async function boot(): Promise<void> {
  if (state.busy) return;
  setBusy('正在连接服务');
  clearGlobalError();
  const results = await Promise.allSettled([api.info(), api.customers()]);
  try {
    const [info, customers] = results;
    if (info.status === 'rejected') throw info.reason;
    if (customers.status === 'rejected') throw customers.reason;
    if (!customers.value.length) throw new ApiError('NO_DEMO_CUSTOMERS', '服务没有提供演示客户，暂时无法创建会话。');
    state.info = info.value;
    state.customers = customers.value;
    state.ready = true;
    dom.customerSelect.replaceChildren(...state.customers.map((customer) => {
      const option = element('option', undefined, customer.name);
      option.value = customer.id;
      return option;
    }));
    required('#runtime-badge').textContent = runtimeLabel(state.info.runtime);
    required('#runtime-badge').title = state.info.model ?? '依据检索到的原文组织回复';
    required('#connection-label').textContent = '服务已连接';
    required('#business-date').textContent = `业务日期 ${state.info.business_date}`;
    dom.app.classList.add('connected');
    showCustomerOrders();
  } catch (error) {
    state.ready = false;
    required('#runtime-badge').textContent = '连接失败';
    required('#connection-label').textContent = '服务未连接';
    required('#business-date').textContent = '业务日期暂不可用';
    dom.customerSelect.replaceChildren(element('option', undefined, '无法读取演示客户'));
    showGlobalError(error, () => { void boot(); });
  } finally {
    setBusy(null);
  }
  if (state.ready && !activeSession()) await createSession(state.customers[0].id);
}

const knowledge = new KnowledgeView({
  catalogChanged: (catalog) => {
    state.catalog = catalog;
    const session = activeSession();
    if (catalog && session && !session.researchModeSelected && session.pendingRequest?.kind !== 'research') {
      session.researchMode = catalog.capabilities.research_default_mode;
    }
    updateControls();
  },
  customerChanged: (id) => { void createSession(id); },
  researchRequested: (snapshotId) => {
    const session = activeSession();
    if (!session) return;
    session.taskMode = 'research';
    session.snapshotId = snapshotId;
    session.pendingRequest = undefined;
    session.sendError = undefined;
    selectView('workspace');
    selectPanel('conversation');
    renderAll();
    dom.input.focus();
  },
  announce,
  downloadError: (error) => showGlobalError(error),
});

function changeResearchSelection(event: Event): void {
  const session = activeSession();
  if (!session) return;
  session.taskMode = dom.taskMode.value as 'support' | 'research';
  session.researchMode = dom.researchMode.value as ResearchMode;
  if (event.currentTarget === dom.researchMode) session.researchModeSelected = true;
  session.snapshotId = dom.researchSnapshot.value || null;
  session.pendingRequest = undefined;
  session.sendError = undefined;
  renderMessageFeedback();
  updateControls();
}

dom.taskMode.addEventListener('change', changeResearchSelection);
dom.researchMode.addEventListener('change', changeResearchSelection);
dom.researchSnapshot.addEventListener('change', changeResearchSelection);

dom.form.addEventListener('submit', (event) => {
  event.preventDefault();
  void sendMessage();
});
dom.input.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    void sendMessage();
  }
});
dom.input.addEventListener('input', () => {
  const session = activeSession();
  if (session) {
    session.draft = dom.input.value;
    if (session.pendingRequest && session.pendingRequest.message !== session.draft.trim()) {
      session.pendingRequest = undefined;
      session.sendError = undefined;
      renderMessageFeedback();
    }
  }
  updateControls();
});
dom.newSession.addEventListener('click', () => { void createSession(dom.customerSelect.value); });
dom.customerSelect.addEventListener('change', () => { void createSession(dom.customerSelect.value); });
dom.refreshEvaluation.addEventListener('click', () => { void loadEvaluation(); });
document.querySelectorAll<HTMLButtonElement>('.rail-button[data-view]').forEach((node) => {
  node.addEventListener('click', () => selectView(node.dataset.view ?? ''));
});
document.querySelectorAll<HTMLButtonElement>('.panel-tabs [data-panel]').forEach((node) => {
  node.addEventListener('click', () => selectPanel(node.dataset.panel as Panel));
});
document.querySelectorAll<HTMLButtonElement>('[data-inspector]').forEach((node) => {
  node.addEventListener('click', () => setInspector(node.dataset.inspector as InspectorTab));
  node.addEventListener('keydown', (event) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const tab: InspectorTab = event.key === 'Home' ? 'evidence' : event.key === 'End' ? 'trace' : node.dataset.inspector === 'evidence' ? 'trace' : 'evidence';
    setInspector(tab, true);
  });
});
bindScenarios(document.querySelector('.sidebar')!);
renderInspector();
void boot();
