import {
  ApiError, array, boolean, choice, documentEvidence, integer, nullableString, record,
  request, responseJson, string, transport,
} from './api';
import type { DocumentEvidence, ResearchMode } from './api';

export const retrievalMethods = ['lexical', 'semantic', 'hybrid', 'graph', 'text2cypher', 'graphrag_local', 'graphrag_global'] as const;
export type RetrievalMethod = typeof retrievalMethods[number];
export const methodLabels: Record<RetrievalMethod, string> = {
  lexical: '词面检索', semantic: '语义检索', hybrid: '混合检索', graph: '关系路径',
  text2cypher: '自然语言图查询', graphrag_local: 'GraphRAG 局部检索', graphrag_global: 'GraphRAG 全局检索',
};

interface StoredError { code: string; message: string }

export interface KnowledgeDocument {
  document_id: string;
  title: string;
  version: number;
  filename: string;
  media_type: string;
  source_sha256: string;
  status: 'parsing' | 'ready' | 'failed';
  block_count: number;
  created_at: string;
  error: StoredError | null;
  idempotent: boolean;
}

export interface KnowledgeSnapshot {
  id: string;
  tenant_id: string;
  status: 'building' | 'published' | 'failed';
  source_count: number;
  with_embeddings: boolean;
  created_at: string;
  finished_at: string | null;
  artifacts: Record<string, unknown>;
  error: StoredError | null;
}

export interface KnowledgeCatalog {
  documents: KnowledgeDocument[];
  snapshots: KnowledgeSnapshot[];
  current_snapshot: KnowledgeSnapshot | null;
  methods: RetrievalMethod[];
  method_status: Partial<Record<RetrievalMethod, { available: boolean; error: StoredError | null }>>;
  capabilities: {
    pdf_parser: string;
    embedding_provider: string;
    embedding_model: string | null;
    graph_backend: string;
    graph_extraction: string;
    microsoft_graphrag: boolean;
    research: boolean;
    research_default_mode: ResearchMode;
    max_upload_bytes: number;
  };
}

export interface SearchResult {
  snapshot_id: string;
  method: RetrievalMethod;
  evidence: DocumentEvidence[];
}

function storedError(value: unknown): StoredError | null {
  if (value === null) return null;
  const item = record(value, 'stored_error');
  return { code: string(item.code, 'error.code'), message: string(item.message, 'error.message') };
}

function document(value: unknown): KnowledgeDocument {
  const item = record(value, 'document');
  return {
    document_id: string(item.document_id, 'document.document_id'),
    title: string(item.title, 'document.title'), version: integer(item.version, 'document.version'),
    filename: string(item.filename, 'document.filename'), media_type: string(item.media_type, 'document.media_type'),
    source_sha256: string(item.source_sha256, 'document.source_sha256'),
    status: choice(item.status, 'document.status', ['parsing', 'ready', 'failed'] as const),
    block_count: integer(item.block_count, 'document.block_count'),
    created_at: string(item.created_at, 'document.created_at'),
    error: storedError(item.error), idempotent: boolean(item.idempotent, 'document.idempotent'),
  };
}

function snapshot(value: unknown): KnowledgeSnapshot {
  const item = record(value, 'snapshot');
  return {
    id: string(item.id, 'snapshot.id'), tenant_id: string(item.tenant_id, 'snapshot.tenant_id'),
    status: choice(item.status, 'snapshot.status', ['building', 'published', 'failed'] as const),
    source_count: integer(item.source_count, 'snapshot.source_count'),
    with_embeddings: boolean(item.with_embeddings, 'snapshot.with_embeddings'),
    created_at: string(item.created_at, 'snapshot.created_at'),
    finished_at: nullableString(item.finished_at, 'snapshot.finished_at'),
    artifacts: record(item.artifacts, 'snapshot.artifacts'), error: storedError(item.error),
  };
}

function methods(value: unknown): RetrievalMethod[] {
  return array(value, 'methods', (item) => choice(item, 'method', retrievalMethods));
}

function catalog(value: unknown): KnowledgeCatalog {
  const item = record(value, 'catalog');
  const capabilities = record(item.capabilities, 'capabilities');
  const status = record(item.method_status, 'method_status');
  const methodStatus = Object.fromEntries(Object.entries(status).map(([name, value]) => {
    const method = choice(name, 'method_status.method', retrievalMethods);
    const state = record(value, 'method_status.state');
    return [method, { available: boolean(state.available, 'method_status.available'), error: storedError(state.error) }];
  }));
  return {
    documents: array(item.documents, 'catalog.documents', document),
    snapshots: array(item.snapshots, 'catalog.snapshots', snapshot),
    current_snapshot: item.current_snapshot === null ? null : snapshot(item.current_snapshot),
    methods: methods(item.methods),
    method_status: methodStatus,
    capabilities: {
      pdf_parser: string(capabilities.pdf_parser, 'capabilities.pdf_parser'),
      embedding_provider: string(capabilities.embedding_provider, 'capabilities.embedding_provider'),
      embedding_model: nullableString(capabilities.embedding_model, 'capabilities.embedding_model'),
      graph_backend: string(capabilities.graph_backend, 'capabilities.graph_backend'),
      graph_extraction: string(capabilities.graph_extraction, 'capabilities.graph_extraction'),
      microsoft_graphrag: boolean(capabilities.microsoft_graphrag, 'capabilities.microsoft_graphrag'),
      research: boolean(capabilities.research, 'capabilities.research'),
      research_default_mode: choice(capabilities.research_default_mode, 'capabilities.research_default_mode', ['single', 'multi'] as const),
      max_upload_bytes: integer(capabilities.max_upload_bytes, 'capabilities.max_upload_bytes'),
    },
  };
}

export const knowledgeApi = {
  catalog: (token: string, signal?: AbortSignal) => request('/knowledge', catalog, token, undefined, signal),
  snapshot: (token: string, id: string, signal?: AbortSignal) => request(
    `/knowledge/snapshots/${encodeURIComponent(id)}`,
    (value) => ({ ...snapshot(value), methods: methods(record(value, 'snapshot').methods) }),
    token, undefined, signal,
  ),
  upload: async (token: string, file: File, title: string, signal?: AbortSignal) => {
    const query = new URLSearchParams({ filename: file.name, title });
    const response = await transport(`/knowledge/documents?${query}`, {
      token, signal, method: 'POST', body: file, contentType: 'application/octet-stream',
    });
    return document(await responseJson(response));
  },
  retry: (token: string, item: KnowledgeDocument, signal?: AbortSignal) => request(
    `/knowledge/documents/${encodeURIComponent(item.document_id)}/versions/${item.version}/retry`,
    document, token, {}, signal,
  ),
  build: (token: string, withEmbeddings: boolean, withGraph: boolean, signal?: AbortSignal) => request(
    '/knowledge/snapshots', snapshot, token,
    { with_embeddings: withEmbeddings, with_graph: withGraph }, signal,
  ),
  demo: (token: string, withEmbeddings: boolean, signal?: AbortSignal) => request(
    '/knowledge/demo', catalog, token, { with_embeddings: withEmbeddings, with_graph: true }, signal,
  ),
  search: (token: string, query: string, method: RetrievalMethod, snapshotId: string, signal?: AbortSignal) => request(
    '/knowledge/search', (value): SearchResult => {
      const item = record(value, 'search');
      const result = {
        snapshot_id: string(item.snapshot_id, 'search.snapshot_id'),
        method: choice(item.method, 'search.method', retrievalMethods),
        evidence: array(item.evidence, 'search.evidence', documentEvidence),
      };
      if (result.snapshot_id !== snapshotId || result.method !== method
        || result.evidence.some((source) => source.snapshot_id !== snapshotId)) {
        throw new ApiError('EVIDENCE_SCOPE', '返回的证据与所选资料版本不一致。');
      }
      return result;
    }, token, { query, method, snapshot_id: snapshotId, limit: 5 }, signal,
  ),
  original: async (
    token: string, documentId: string, version: number, sha256: string, signal?: AbortSignal,
  ): Promise<{ blob: Blob; filename: string }> => {
    const response = await transport(
      `/knowledge/documents/${encodeURIComponent(documentId)}/versions/${version}/source`,
      { token, signal, accept: 'application/octet-stream' },
    );
    if (response.headers.get('X-Source-SHA256') !== sha256) {
      throw new ApiError('SOURCE_INTEGRITY', '下载原件的 SHA-256 与所选来源不一致。');
    }
    const disposition = response.headers.get('Content-Disposition') ?? '';
    const match = /^attachment;\s*filename\*=UTF-8''(.+)$/i.exec(disposition);
    if (!match) throw new ApiError('INVALID_RESPONSE', '原件响应缺少有效文件名。');
    let filename: string;
    try { filename = decodeURIComponent(match[1]); }
    catch { throw new ApiError('INVALID_RESPONSE', '原件文件名编码无效。'); }
    return { blob: await response.blob(), filename };
  },
};
