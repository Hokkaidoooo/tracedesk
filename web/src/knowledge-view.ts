import { ApiError, asApiError } from './api';
import type { CreatedSession, DemoCustomer, DocumentEvidence } from './api';
import { errorBlock, renderCitations } from './components';
import { button, element, required, timestamp } from './dom';
import { knowledgeApi, methodLabels, retrievalMethods } from './knowledge-api';
import type { KnowledgeCatalog, KnowledgeDocument, KnowledgeSnapshot, RetrievalMethod, SearchResult } from './knowledge-api';

interface Scope { id: string; token: string; generation: number }
interface Callbacks {
  catalogChanged: (catalog: KnowledgeCatalog | null) => void;
  customerChanged: (id: string) => void;
  researchRequested: (snapshotId: string) => void;
  announce: (message: string) => void;
  downloadError: (error: ApiError) => void;
}

export function snapshotLabel(snapshot: KnowledgeSnapshot, currentId?: string): string {
  const marker = snapshot.id === currentId ? '当前 · ' : '';
  return `${marker}${timestamp(snapshot.created_at)} · ${snapshot.id.slice(0, 8)} · ${snapshot.source_count} 条来源`;
}

export class KnowledgeView {
  private readonly root = required('#knowledge-view');
  private readonly customer = required<HTMLSelectElement>('#knowledge-customer');
  private readonly uploadForm = required<HTMLFormElement>('#knowledge-upload-form');
  private readonly file = required<HTMLInputElement>('#knowledge-file');
  private readonly title = required<HTMLInputElement>('#knowledge-document-title');
  private readonly embeddings = required<HTMLInputElement>('#knowledge-build-embeddings');
  private readonly graph = required<HTMLInputElement>('#knowledge-build-graph');
  private readonly snapshot = required<HTMLSelectElement>('#knowledge-snapshot');
  private readonly method = required<HTMLSelectElement>('#knowledge-method');
  private readonly query = required<HTMLInputElement>('#knowledge-query');
  private readonly feedback = required('#knowledge-feedback');
  private readonly controllers = new Set<AbortController>();
  private session: CreatedSession | null = null;
  private generation = 0;
  private busy: string | null = null;
  private identityBusy = true;
  private catalog: KnowledgeCatalog | null = null;
  private selectedId: string | null = null;
  private availableMethods: RetrievalMethod[] = [];
  private results: SearchResult | null = null;
  private errors: ApiError[] = [];
  private notice = '';

  constructor(private readonly callbacks: Callbacks) {
    this.customer.addEventListener('change', () => callbacks.customerChanged(this.customer.value));
    this.uploadForm.addEventListener('submit', (event) => {
      event.preventDefault();
      void this.upload();
    });
    required<HTMLFormElement>('#knowledge-search-form').addEventListener('submit', (event) => {
      event.preventDefault();
      void this.search();
    });
    required('#knowledge-refresh').addEventListener('click', () => { void this.refresh(); });
    required('#knowledge-build').addEventListener('click', () => { void this.build(); });
    required('#knowledge-demo').addEventListener('click', () => { void this.demo(); });
    required('#knowledge-use-research').addEventListener('click', () => {
      if (this.selectedId) callbacks.researchRequested(this.selectedId);
    });
    this.snapshot.addEventListener('change', () => {
      this.selectedId = this.snapshot.value || null;
      this.results = null;
      void this.run('正在读取资料版本', (scope, signal) => this.loadMethods(scope, signal), false);
    });
    this.file.addEventListener('change', () => {
      const file = this.file.files?.[0];
      if (file && !this.title.value) this.title.value = file.name.replace(/\.[^.]+$/, '').slice(0, 200);
    });
  }

  setIdentityBusy(busy: boolean): void {
    this.identityBusy = busy;
    this.customer.disabled = busy || this.customer.options.length === 0;
  }

  setSession(session: CreatedSession | null, customers: DemoCustomer[], selectedCustomer: string): void {
    this.generation += 1;
    for (const controller of this.controllers) controller.abort();
    this.controllers.clear();
    this.session = session;
    this.busy = null;
    this.catalog = null;
    this.selectedId = null;
    this.availableMethods = [];
    this.results = null;
    this.errors = [];
    this.notice = '';
    this.uploadForm.reset();
    this.query.value = '';
    this.embeddings.checked = false;
    this.graph.checked = false;
    this.customer.replaceChildren(...customers.map((customer) => {
      const option = element('option', undefined, customer.name);
      option.value = customer.id;
      return option;
    }));
    this.customer.value = session?.customer.id ?? selectedCustomer;
    this.callbacks.catalogChanged(null);
    this.render();
    if (session) void this.refresh();
  }

  private scope(): Scope | null {
    return this.session ? { id: this.session.id, token: this.session.token, generation: this.generation } : null;
  }

  private current(scope: Scope): boolean {
    return scope.generation === this.generation && scope.id === this.session?.id;
  }

  private async run(
    label: string, action: (scope: Scope, signal: AbortSignal) => Promise<void>,
    reload = true, reportDownloadError = false,
  ): Promise<void> {
    const scope = this.scope();
    if (!scope || this.busy) return;
    const controller = new AbortController();
    this.controllers.add(controller);
    this.busy = label;
    this.errors = [];
    this.notice = '';
    this.render();
    try {
      await action(scope, controller.signal);
    } catch (error) {
      if (this.current(scope)) {
        const failure = asApiError(error);
        this.errors.push(failure);
        if (reportDownloadError) this.callbacks.downloadError(failure);
        this.callbacks.announce(label + '失败：' + failure.message);
      }
    } finally {
      if (this.current(scope) && reload) {
        try { await this.fetchCatalog(scope, controller.signal); }
        catch (error) { if (this.current(scope)) this.errors.push(asApiError(error)); }
      }
      this.controllers.delete(controller);
      if (this.current(scope)) {
        this.busy = null;
        this.render();
        if (!this.errors.length && this.notice) this.callbacks.announce(this.notice);
      }
    }
  }

  async refresh(): Promise<void> {
    return this.run('正在读取资料库', (scope, signal) => this.fetchCatalog(scope, signal), false);
  }

  private async fetchCatalog(scope: Scope, signal: AbortSignal): Promise<void> {
    const catalog = await knowledgeApi.catalog(scope.token, signal);
    if (!this.current(scope)) return;
    this.catalog = catalog;
    if (!catalog.snapshots.some((item) => item.id === this.selectedId && item.status === 'published')) {
      this.selectedId = catalog.current_snapshot?.id ?? null;
      this.results = null;
    }
    await this.loadMethods(scope, signal);
    if (this.current(scope)) this.callbacks.catalogChanged(catalog);
  }

  private async loadMethods(scope: Scope, signal: AbortSignal): Promise<void> {
    const id = this.selectedId;
    if (!id) {
      this.availableMethods = [];
      return;
    }
    const snapshot = await knowledgeApi.snapshot(scope.token, id, signal);
    if (this.current(scope) && this.selectedId === id) this.availableMethods = snapshot.methods;
  }

  private async upload(): Promise<void> {
    const file = this.file.files?.[0];
    const title = this.title.value.trim();
    if (!file || !title) return;
    return this.run('正在上传并解析', async (scope, signal) => {
      const document = await knowledgeApi.upload(scope.token, file, title, signal);
      if (!this.current(scope)) return;
      if (document.status === 'failed') {
        throw new ApiError(document.error?.code ?? 'DOCUMENT_FAILED', document.error?.message ?? '该原件上次解析失败，请使用“重试解析”。');
      }
      this.notice = document.status === 'ready'
        ? `${document.title} v${document.version} 已解析，可构建并发布。`
        : '同一原件正在解析，请刷新查看结果。';
      this.uploadForm.reset();
    });
  }

  private async retry(document: KnowledgeDocument): Promise<void> {
    return this.run('正在重试解析', async (scope, signal) => {
      const result = await knowledgeApi.retry(scope.token, document, signal);
      if (this.current(scope)) this.notice = `${result.title} v${result.version} 已重新解析。`;
    });
  }

  private async build(): Promise<void> {
    const embeddings = this.embeddings.checked;
    const graph = this.graph.checked;
    return this.run('正在构建并发布', async (scope, signal) => {
      const snapshot = await knowledgeApi.build(scope.token, embeddings, graph, signal);
      if (!this.current(scope)) return;
      this.selectedId = snapshot.id;
      this.results = null;
      this.notice = '新的资料版本已发布。';
    });
  }

  private async demo(): Promise<void> {
    const embeddings = this.embeddings.checked;
    return this.run('正在加载示例资料', async (scope, signal) => {
      const catalog = await knowledgeApi.demo(scope.token, embeddings, signal);
      if (!this.current(scope)) return;
      this.selectedId = catalog.current_snapshot?.id ?? null;
      this.results = null;
      this.notice = '示例资料及其关系已发布，可开始检索。';
    });
  }

  private async search(): Promise<void> {
    const id = this.selectedId;
    const query = this.query.value.trim();
    const method = this.method.value as RetrievalMethod;
    if (!id || !query) return;
    return this.run('正在检索', async (scope, signal) => {
      const result = await knowledgeApi.search(scope.token, query, method, id, signal);
      if (!this.current(scope) || this.selectedId !== id) return;
      this.results = result;
      this.notice = `找到 ${result.evidence.length} 条来源。`;
    }, false);
  }

  async downloadEvidence(source: DocumentEvidence): Promise<void> {
    return this.download(source.document_id, source.document_version, source.source_sha256);
  }

  private async download(documentId: string, version: number, sha256: string): Promise<void> {
    return this.run('正在下载原件', async (scope, signal) => {
      const result = await knowledgeApi.original(scope.token, documentId, version, sha256, signal);
      if (!this.current(scope)) return;
      const url = URL.createObjectURL(result.blob);
      const link = element('a');
      link.href = url;
      link.download = result.filename;
      link.hidden = true;
      document.body.append(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      this.notice = '已开始下载原件：' + result.filename;
    }, false, true);
  }

  private render(): void {
    this.root.setAttribute('aria-busy', String(this.busy !== null));
    this.feedback.replaceChildren(...this.errors.map((error) => errorBlock(error)));
    if (this.busy) this.feedback.append(element('p', 'knowledge-progress', this.busy + '…'));
    else if (this.notice) this.feedback.append(element('p', 'knowledge-notice', this.notice));
    this.renderCapabilities();
    this.renderDocuments();
    this.renderSnapshots();
    const results = required('#knowledge-results');
    results.replaceChildren(this.results
      ? renderCitations(this.results.evidence, (source) => { void this.downloadEvidence(source); })
      : element('p', 'knowledge-empty', this.selectedId ? '输入问题，检索这个版本的原文。' : '发布资料后，可在这里检索原文。'));
    if (this.results && !this.results.evidence.length) {
      results.replaceChildren(element('p', 'knowledge-empty', '没有找到匹配原文。请调整问题或选择其他资料版本。'));
    }
    this.renderControls();
  }

  private renderCapabilities(): void {
    const capabilities = this.catalog?.capabilities;
    const descriptions = capabilities ? [
      '文本资料可用',
      capabilities.pdf_parser === 'none' ? 'PDF 解析未启用' : 'PDF 解析可用',
      capabilities.embedding_provider === 'none' ? '语义检索未启用' : '语义检索可用',
      capabilities.research ? '资料研究可用' : '资料研究未启用',
    ] : [this.session ? '正在读取资料能力' : '请选择演示身份'];
    required('#knowledge-capabilities').replaceChildren(...descriptions.map((text) => element('span', 'capability-pill', text)));
    const maximum = capabilities ? (capabilities.max_upload_bytes / 1024 / 1024).toFixed(0) : '—';
    required('#knowledge-upload-hint').textContent = `支持 UTF-8 TXT、Markdown 和 PDF，最大 ${maximum} MB。`;
    required('#knowledge-build-hint').textContent = capabilities?.graph_extraction === 'model'
      ? '关系图从资料中抽取，并保留原文依据。'
      : '自动关系抽取未启用。示例资料包含可直接使用的来源关系。';
  }

  private renderDocuments(): void {
    const documents = this.catalog?.documents ?? [];
    required('#knowledge-document-count').textContent = `${documents.length} 个版本`;
    const list = required('#knowledge-documents');
    if (!documents.length) {
      list.replaceChildren(element('p', 'knowledge-empty', this.session ? '还没有资料。上传文档，或加载示例资料。' : '连接会话后可查看资料。'));
      return;
    }
    list.replaceChildren(...documents.map((item) => {
      const row = element('article', 'knowledge-document');
      row.dataset.testid = 'knowledge-document';
      row.dataset.documentId = item.document_id;
      row.dataset.version = String(item.version);
      const heading = element('div', 'document-heading');
      const title = element('div');
      title.append(element('h3', undefined, item.title), element('p', 'document-meta', `${item.filename} · v${item.version} · ${item.block_count} 条原文`));
      const labels = { ready: '已解析', parsing: '解析中', failed: '解析失败' };
      heading.append(title, element('span', 'document-status state-' + item.status, labels[item.status]));
      row.append(heading);
      if (item.error) row.append(errorBlock(new ApiError(item.error.code, item.error.message)));
      const actions = element('div', 'document-actions');
      actions.append(button('下载原件', 'text-button', () => { void this.download(item.document_id, item.version, item.source_sha256); }));
      if (item.status === 'failed') actions.append(button('重试解析', 'button-secondary', () => { void this.retry(item); }));
      row.append(actions);
      return row;
    }));
  }

  private renderSnapshots(): void {
    const previousMethod = this.method.value as RetrievalMethod;
    const current = this.catalog?.current_snapshot;
    const published = this.catalog?.snapshots.filter((item) => item.status === 'published') ?? [];
    this.snapshot.replaceChildren(...published.map((item) => {
      const option = element('option', undefined, snapshotLabel(item, current?.id));
      option.value = item.id;
      return option;
    }));
    if (!published.length) this.snapshot.append(element('option', undefined, '尚无已发布资料'));
    this.snapshot.value = this.selectedId ?? '';
    this.method.replaceChildren(...retrievalMethods.map((method) => {
      const option = element('option', undefined, methodLabels[method] + (this.availableMethods.includes(method) ? '' : ' · 未启用'));
      option.value = method;
      option.disabled = !this.availableMethods.includes(method);
      return option;
    }));
    this.method.value = this.availableMethods.includes(previousMethod) ? previousMethod : this.availableMethods[0] ?? 'lexical';
    required('#knowledge-current').textContent = current
      ? '当前发布：' + snapshotLabel(current) : '尚无已发布资料。';
    const readiness = required('#knowledge-method-status');
    const messages = new Set<string>();
    if (this.selectedId === current?.id) {
      for (const status of Object.values(this.catalog?.method_status ?? {})) {
        if (status?.error && !status.available && /mismatch|changed|integrity|invalid|dependency|model_format/i.test(status.error.code)) {
          messages.add(status.error.message);
        }
      }
    }
    readiness.replaceChildren(...[...messages].map((message) => element('p', undefined, message)));
    readiness.hidden = messages.size === 0;
    const history = this.catalog?.snapshots ?? [];
    required('#knowledge-history').replaceChildren(...history.map((item) => {
      const row = element('div', 'snapshot-record');
      const labels = { building: '构建中', published: '已发布', failed: '构建失败' };
      row.append(element('strong', undefined, labels[item.status] + ' · ' + item.id.slice(0, 8)), element('p', 'document-meta', timestamp(item.created_at)));
      if (item.error) row.append(errorBlock(new ApiError(item.error.code, item.error.message)));
      return row;
    }));
    if (!history.length) required('#knowledge-history').append(element('p', 'knowledge-empty', '暂无发布记录。'));
  }

  private renderControls(): void {
    const unavailable = !this.session || this.busy !== null;
    const capabilities = this.catalog?.capabilities;
    const canEmbed = capabilities !== undefined && capabilities.embedding_provider !== 'none';
    const canExtract = capabilities?.graph_extraction === 'model' && capabilities.research;
    this.customer.disabled = this.identityBusy;
    this.file.disabled = unavailable;
    this.title.disabled = unavailable;
    this.embeddings.disabled = unavailable || !canEmbed;
    this.graph.disabled = unavailable || !canExtract;
    if (!canEmbed) this.embeddings.checked = false;
    if (!canExtract) this.graph.checked = false;
    this.snapshot.disabled = unavailable || !this.selectedId;
    this.method.disabled = unavailable || !this.selectedId;
    this.query.disabled = unavailable || !this.selectedId;
    required<HTMLButtonElement>('#knowledge-upload').disabled = unavailable;
    required<HTMLButtonElement>('#knowledge-refresh').disabled = unavailable;
    required<HTMLButtonElement>('#knowledge-build').disabled = unavailable || !this.catalog?.documents.length;
    required<HTMLButtonElement>('#knowledge-demo').disabled = unavailable || !this.catalog;
    required<HTMLButtonElement>('#knowledge-search').disabled = unavailable || !this.selectedId;
    required<HTMLButtonElement>('#knowledge-use-research').disabled = unavailable || !this.selectedId || !capabilities?.research;
    this.root.querySelectorAll<HTMLButtonElement>('.document-actions button, .source-download').forEach((node) => { node.disabled = unavailable; });
  }
}
