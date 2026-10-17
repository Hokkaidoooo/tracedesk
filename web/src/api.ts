export type ResearchMode = 'single' | 'multi';
export type Runtime = 'extractive' | 'llm' | `research:${ResearchMode}:${string}`;
export type TurnStatus = 'answered' | 'needs_confirmation' | 'needs_information' | 'handoff' | 'partially_supported' | 'insufficient_evidence';
export type ActionState = 'pending' | 'submitted' | 'cancelled' | 'expired';

export interface AppInfo {
  name: string;
  version: string;
  runtime: Runtime;
  model: string | null;
  demo: boolean;
  business_date: string;
}

export interface Customer {
  id: string;
  name: string;
}

export interface DemoCustomer extends Customer {
  order_ids: string[];
}

export interface CreatedSession {
  id: string;
  token: string;
  customer: Customer;
  created_at: string;
}

export interface PolicyCitation {
  source_id: string;
  title: string;
  quote: string;
  version: number;
  effective_from: string;
  effective_to: string | null;
  score: number | null;
}

export interface DocumentEvidence {
  source_id: string;
  tenant_id: string;
  snapshot_id: string;
  document_id: string;
  document_version: number;
  title: string;
  text: string;
  quote: string;
  source_sha256: string;
  page: number | null;
  kind: 'text' | 'table';
  score: number | null;
  method: string;
  graph_path: string[];
  claim_id?: string;
}

export type Citation = PolicyCitation | DocumentEvidence;

export function isDocumentEvidence(citation: Citation): citation is DocumentEvidence {
  return 'snapshot_id' in citation;
}

export interface Order {
  id: string;
  product: string;
  amount_minor: number;
  currency: string;
  status: string;
  delivered_at: string | null;
  version: number;
}

export interface RefundAction {
  id: string;
  kind: 'refund_request';
  state: ActionState;
  order_id: string;
  amount_minor: number;
  currency: string;
  reason: string;
  expires_at: string;
  confirmation_token?: string;
  receipt_id?: string;
  submitted_at?: string;
}

export interface TraceEvent {
  seq: number;
  stage: string;
  label: string;
  status: string;
  detail: string;
  duration_ms: number;
}

export interface Turn {
  id: string;
  run_id: string;
  request_id: string;
  message: string;
  answer: string;
  status: TurnStatus;
  runtime: Runtime;
  citations: Citation[];
  order: Order | null;
  action: RefundAction | null;
  trace: TraceEvent[];
  created_at: string;
}

export interface LoadedSession {
  id: string;
  customer: Customer;
  turns: Turn[];
}

export interface Receipt {
  id: string;
  state: 'submitted';
  order_id: string;
  amount_minor: number;
  currency: string;
  receipt_id: string;
  submitted_at: string;
  replayed: boolean;
}

export interface RetrievalMetrics {
  queries: number;
  answerable: number;
  recall_at_3: number;
  mrr: number;
  no_answer_accuracy: number;
  latency_ms_p50: number;
}

export interface EvaluationReport {
  generated_at: string;
  scope: string;
  dataset_sha256: string;
  retrieval: Record<string, RetrievalMetrics>;
  workflow: { total: number; passed: number; cases: WorkflowCase[] };
}

export interface WorkflowCase {
  id: string;
  name: string;
  passed: boolean;
  expected: string;
  actual: string;
}

export class ApiError extends Error {
  constructor(readonly code: string, message: string, readonly status = 0) {
    super(message);
    this.name = 'ApiError';
  }
}

type RecordValue = Record<string, unknown>;

export function invalid(field: string): never {
  throw new ApiError('INVALID_RESPONSE', `服务响应字段 ${field} 不符合接口约定，请检查服务状态。`);
}

export function record(value: unknown, field: string): RecordValue {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) invalid(field);
  return value as RecordValue;
}

export function string(value: unknown, field: string): string {
  if (typeof value !== 'string') invalid(field);
  return value;
}

export function number(value: unknown, field: string): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) invalid(field);
  return value;
}

export function integer(value: unknown, field: string): number {
  const result = number(value, field);
  if (!Number.isSafeInteger(result) || result < 0) invalid(field);
  return result;
}

export function boolean(value: unknown, field: string): boolean {
  if (typeof value !== 'boolean') invalid(field);
  return value;
}

export function array<T>(value: unknown, field: string, decode: (item: unknown) => T): T[] {
  if (!Array.isArray(value)) invalid(field);
  return value.map(decode);
}

export function choice<T extends string>(value: unknown, field: string, choices: readonly T[]): T {
  const result = string(value, field);
  if (!choices.includes(result as T)) invalid(field);
  return result as T;
}

export function optionalString(value: unknown, field: string): string | undefined {
  return value === undefined || value === null ? undefined : string(value, field);
}

export function nullableString(value: unknown, field: string): string | null {
  return value === null ? null : string(value, field);
}

function customer(value: unknown): Customer {
  const item = record(value, 'customer');
  return { id: string(item.id, 'customer.id'), name: string(item.name, 'customer.name') };
}

function citation(value: unknown): Citation {
  const item = record(value, 'citation');
  if ('snapshot_id' in item) return documentEvidence(item);
  return {
    source_id: string(item.source_id, 'citation.source_id'),
    title: string(item.title, 'citation.title'),
    quote: string(item.quote, 'citation.quote'),
    version: integer(item.version, 'citation.version'),
    effective_from: string(item.effective_from, 'citation.effective_from'),
    effective_to: nullableString(item.effective_to, 'citation.effective_to'),
    score: item.score === null ? null : number(item.score, 'citation.score'),
  };
}

export function documentEvidence(value: unknown): DocumentEvidence {
  const item = record(value, 'evidence');
  const page = item.page === null ? null : integer(item.page, 'evidence.page');
  if (page === 0) invalid('evidence.page');
  const digest = string(item.source_sha256, 'evidence.source_sha256');
  if (!/^[a-f0-9]{64}$/.test(digest)) invalid('evidence.source_sha256');
  return {
    source_id: string(item.source_id, 'evidence.source_id'),
    tenant_id: string(item.tenant_id, 'evidence.tenant_id'),
    snapshot_id: string(item.snapshot_id, 'evidence.snapshot_id'),
    document_id: string(item.document_id, 'evidence.document_id'),
    document_version: integer(item.document_version, 'evidence.document_version'),
    title: string(item.title, 'evidence.title'),
    text: string(item.text, 'evidence.text'),
    quote: string(item.quote, 'evidence.quote'),
    source_sha256: digest, page,
    kind: choice(item.kind, 'evidence.kind', ['text', 'table'] as const),
    score: item.score === null ? null : number(item.score, 'evidence.score'),
    method: string(item.method, 'evidence.method'),
    graph_path: array(item.graph_path, 'evidence.graph_path', (entry) => string(entry, 'graph_path.entry')),
    claim_id: optionalString(item.claim_id, 'evidence.claim_id'),
  };
}

function runtime(value: unknown): Runtime {
  const name = string(value, 'turn.runtime');
  if (name === 'extractive' || name === 'llm' || /^research:(single|multi):[^:]+$/.test(name)) return name as Runtime;
  return invalid('turn.runtime');
}

function order(value: unknown): Order {
  const item = record(value, 'order');
  return {
    id: string(item.id, 'order.id'),
    product: string(item.product, 'order.product'),
    amount_minor: integer(item.amount_minor, 'order.amount_minor'),
    currency: string(item.currency, 'order.currency'),
    status: string(item.status, 'order.status'),
    delivered_at: nullableString(item.delivered_at, 'order.delivered_at'),
    version: integer(item.version, 'order.version'),
  };
}

function action(value: unknown): RefundAction {
  const item = record(value, 'action');
  const state = choice(item.state, 'action.state', ['pending', 'submitted', 'cancelled', 'expired'] as const);
  const confirmationToken = optionalString(item.confirmation_token, 'action.confirmation_token');
  if (state === 'pending' && !confirmationToken) invalid('action.confirmation_token');
  return {
    id: string(item.id, 'action.id'),
    kind: choice(item.kind, 'action.kind', ['refund_request'] as const),
    state,
    order_id: string(item.order_id, 'action.order_id'),
    amount_minor: integer(item.amount_minor, 'action.amount_minor'),
    currency: string(item.currency, 'action.currency'),
    reason: string(item.reason, 'action.reason'),
    expires_at: string(item.expires_at, 'action.expires_at'),
    confirmation_token: confirmationToken,
    receipt_id: optionalString(item.receipt_id, 'action.receipt_id'),
    submitted_at: optionalString(item.submitted_at, 'action.submitted_at'),
  };
}

function traceEvent(value: unknown): TraceEvent {
  const item = record(value, 'trace');
  return {
    seq: integer(item.seq, 'trace.seq'),
    stage: string(item.stage, 'trace.stage'),
    label: string(item.label, 'trace.label'),
    status: string(item.status, 'trace.status'),
    detail: string(item.detail, 'trace.detail'),
    duration_ms: number(item.duration_ms, 'trace.duration_ms'),
  };
}

function turn(value: unknown): Turn {
  const item = record(value, 'turn');
  return {
    id: string(item.id, 'turn.id'),
    run_id: string(item.run_id, 'turn.run_id'),
    request_id: string(item.request_id, 'turn.request_id'),
    message: string(item.message, 'turn.message'),
    answer: string(item.answer, 'turn.answer'),
    status: choice(item.status, 'turn.status', ['answered', 'needs_confirmation', 'needs_information', 'handoff', 'partially_supported', 'insufficient_evidence'] as const),
    runtime: runtime(item.runtime),
    citations: array(item.citations, 'turn.citations', citation),
    order: item.order === null ? null : order(item.order),
    action: item.action === null ? null : action(item.action),
    trace: array(item.trace, 'turn.trace', traceEvent),
    created_at: string(item.created_at, 'turn.created_at'),
  };
}

function loadedSession(value: unknown): LoadedSession {
  const item = record(value, 'session');
  return {
    id: string(item.id, 'session.id'),
    customer: customer(item.customer),
    turns: array(item.turns, 'session.turns', turn),
  };
}

function receipt(value: unknown): Receipt {
  const item = record(value, 'receipt');
  return {
    id: string(item.id, 'receipt.id'),
    state: choice(item.state, 'receipt.state', ['submitted'] as const),
    order_id: string(item.order_id, 'receipt.order_id'),
    amount_minor: integer(item.amount_minor, 'receipt.amount_minor'),
    currency: string(item.currency, 'receipt.currency'),
    receipt_id: string(item.receipt_id, 'receipt.receipt_id'),
    submitted_at: string(item.submitted_at, 'receipt.submitted_at'),
    replayed: boolean(item.replayed, 'receipt.replayed'),
  };
}

function evaluation(value: unknown): EvaluationReport {
  const item = record(value, 'evaluation');
  const retrieval = record(item.retrieval, 'evaluation.retrieval');
  const workflow = record(item.workflow, 'evaluation.workflow');
  const methods = Object.fromEntries(Object.entries(retrieval).map(([name, value]) => {
    const metrics = record(value, `retrieval.${name}`);
    return [name, {
      queries: integer(metrics.queries, 'retrieval.queries'),
      answerable: integer(metrics.answerable, 'retrieval.answerable'),
      recall_at_3: number(metrics.recall_at_3, 'retrieval.recall_at_3'),
      mrr: number(metrics.mrr, 'retrieval.mrr'),
      no_answer_accuracy: number(metrics.no_answer_accuracy, 'retrieval.no_answer_accuracy'),
      latency_ms_p50: number(metrics.latency_ms_p50, 'retrieval.latency_ms_p50'),
    }];
  }));
  return {
    generated_at: string(item.generated_at, 'evaluation.generated_at'),
    scope: string(item.scope, 'evaluation.scope'),
    dataset_sha256: string(item.dataset_sha256, 'evaluation.dataset_sha256'),
    retrieval: methods,
    workflow: {
      total: integer(workflow.total, 'workflow.total'),
      passed: integer(workflow.passed, 'workflow.passed'),
      cases: array(workflow.cases, 'workflow.cases', (entry): WorkflowCase => {
        const item = record(entry, 'workflow.case');
        return {
          id: string(item.id, 'workflow.case.id'), name: string(item.name, 'workflow.case.name'),
          passed: boolean(item.passed, 'workflow.case.passed'),
          expected: string(item.expected, 'workflow.case.expected'), actual: string(item.actual, 'workflow.case.actual'),
        };
      }),
    },
  };
}

export interface TransportOptions {
  token?: string;
  body?: BodyInit;
  method?: 'GET' | 'POST';
  contentType?: string;
  accept?: string;
  signal?: AbortSignal;
}

export async function transport(path: string, options: TransportOptions = {}): Promise<Response> {
  const headers: Record<string, string> = { Accept: options.accept ?? 'application/json' };
  if (options.token) headers.Authorization = `Bearer ${options.token}`;
  if (options.contentType) headers['Content-Type'] = options.contentType;
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      method: options.method ?? 'GET', headers, body: options.body, signal: options.signal,
      cache: 'no-store',
      credentials: 'omit',
      redirect: 'error',
    });
  } catch {
    throw new ApiError('NETWORK_ERROR', '无法连接服务。请检查连接后重试；本次输入仍然保留。');
  }
  if (!response.ok) {
    const envelope = record(await responseJson(response), 'error response');
    const error = record(envelope.error, 'error');
    throw new ApiError(string(error.code, 'error.code'), string(error.message, 'error.message'), response.status);
  }
  return response;
}

export async function responseJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    throw new ApiError('INVALID_RESPONSE', `服务返回了无法解析的响应（HTTP ${response.status}）。`, response.status);
  }

}

export async function request<T>(
  path: string, decode: (data: unknown) => T, token?: string, body?: unknown, signal?: AbortSignal,
): Promise<T> {
  const response = await transport(path, {
    token, signal, method: body === undefined ? 'GET' : 'POST',
    body: body === undefined ? undefined : JSON.stringify(body),
    contentType: body === undefined ? undefined : 'application/json',
  });
  return decode(await responseJson(response));
}

export function asApiError(error: unknown): ApiError {
  if (error instanceof ApiError) return error;
  if (error instanceof Error) return new ApiError('CLIENT_ERROR', error.message);
  return new ApiError('CLIENT_ERROR', '页面遇到未知错误，请重新连接后重试。');
}

export const api = {
  info: () => request('/info', (value): AppInfo => {
    const item = record(value, 'info');
    return {
      name: string(item.name, 'info.name'), version: string(item.version, 'info.version'),
      runtime: choice(item.runtime, 'info.runtime', ['extractive', 'llm'] as const),
      model: nullableString(item.model, 'info.model'), demo: boolean(item.demo, 'info.demo'),
      business_date: string(item.business_date, 'info.business_date'),
    };
  }),
  customers: () => request('/demo/customers', (value) => {
    const item = record(value, 'customers');
    return array(item.customers, 'customers', (value): DemoCustomer => {
      const item = record(value, 'customer');
      return { ...customer(item), order_ids: array(item.order_ids, 'customer.order_ids', (id) => string(id, 'order_id')) };
    });
  }),
  createSession: (customerId: string) => request('/sessions', (value): CreatedSession => {
    const item = record(value, 'session');
    return {
      id: string(item.id, 'session.id'), token: string(item.token, 'session.token'),
      customer: customer(item.customer), created_at: string(item.created_at, 'session.created_at'),
    };
  }, undefined, { customer_id: customerId }),
  session: (id: string, token: string) => request(`/sessions/${encodeURIComponent(id)}`, loadedSession, token),
  sendTurn: (id: string, token: string, message: string, requestId: string) => request(
    `/sessions/${encodeURIComponent(id)}/turns`, turn, token, { message, request_id: requestId },
  ),
  research: (id: string, token: string, message: string, requestId: string, mode: ResearchMode, snapshotId: string) => request(
    `/sessions/${encodeURIComponent(id)}/research`, turn, token,
    { message, request_id: requestId, mode, snapshot_id: snapshotId },
  ),
  confirm: (id: string, token: string, confirmationToken: string, requestId: string) => request(
    `/actions/${encodeURIComponent(id)}/confirm`, receipt, token, { confirmation_token: confirmationToken, request_id: requestId },
  ),
  cancel: (id: string, token: string) => request(`/actions/${encodeURIComponent(id)}/cancel`, (value) => {
    const item = record(value, 'cancel');
    return { id: string(item.id, 'cancel.id'), state: choice(item.state, 'cancel.state', ['cancelled'] as const) };
  }, token, {}),
  evaluation: () => request('/evaluation', evaluation),
};
