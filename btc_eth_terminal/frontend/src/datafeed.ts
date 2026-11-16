import type { Bar, BarsResult, Datafeed, Resolution, SymbolInfo, SymbolSearchResult } from '@funnest/charts';

export interface Dataset extends SymbolInfo {
  file: string;
  resolution: string;
  source_type: 'synthetic' | 'real';
  evidence_status: 'unverified' | 'source_audited' | 'oos_tested';
  csv_sha256: string;
  original_raw_sha256?: string;
  fills_sha256?: string | null;
  signals_sha256?: string | null;
  fills_file?: string;
  signals_file?: string;
  provenance_file?: string;
  row_count: number;
  earliest_time: number;
  latest_time: number;
  supports_asof: boolean;
  availability_source_truth_verified: false;
  supportedResolutions: string[];
  evidence_label_is_self_declared: true;
}
export interface Research {
  id: string;
  market: string;
  status: string;
  validated_executable_alpha: boolean;
  description: string;
  next_gate?: string;
  ledger_as_of?: string;
  ledger_sha256?: string;
  report_as_of?: string;
  report_sha256?: string;
}
export interface FrozenExperiment {
  id: string;
  status: string;
  window: string[];
  source_repo: string;
  historical_source_commit: string;
  raw_market_data_reaudited_in_this_import: false;
  strategy: { run_digest: string; metrics: Record<string, string> };
  passive: { run_digest: string; metrics: Record<string, string> };
  summary_sha256: string;
  notes: string[];
}
export interface Catalog {
  experiments: FrozenExperiment[];
  datasets: Dataset[];
  research: Research[];
  trading_enabled: false;
  historical_only: true;
  derived_data_not_strategy_evidence: true;
}
export interface Fill { time: number; price: number; side: 'buy' | 'sell'; quantity: number; event_type: 'paper_fill' | 'real_fill'; available_at?: number }
export interface Signal {
  signal_id: string;
  time: number;
  available_at: number;
  feature_max_observed_at: number;
  side: 'LONG' | 'SHORT' | 'WAIT' | 'FLAT';
  model_id: string;
  record_type: 'CANDIDATE_SIGNAL';
}

async function getJson<T>(url: string): Promise<T> {
  const response = await fetch(url, { cache: 'no-store' });
  const payload: unknown = await response.json();
  if (!response.ok) {
    const msg = payload && typeof payload === 'object' && 'error' in payload && typeof payload.error === 'string'
      ? payload.error : `HTTP ${response.status}`;
    throw new Error(msg);
  }
  return payload as T;
}

export function datasetKey(ds: Dataset): string { return `${ds.symbol}|${ds.resolution}`; }
export async function loadCatalog(): Promise<Catalog> { return getJson<Catalog>('/api/catalog'); }
export function toIso(ms: number): string { return new Date(ms).toISOString(); }
function auditQuery(asof: number | null): string { return asof === null ? '' : `&asof=${asof}`; }
function assertDigest(found: string | null | undefined, expected: string | null | undefined, kind: string) {
  if ((found ?? null) !== (expected ?? null)) {
    throw new Error(`${kind} file changed since catalog load. Refresh to re-audit SHA-256.`);
  }
}

export async function loadFills(dataset: Dataset, asof: number | null): Promise<Fill[]> {
  const result = await getJson<{ fills: Fill[]; fills_sha256: string | null }>(
    `/api/fills?symbol=${encodeURIComponent(dataset.symbol)}${auditQuery(asof)}`);
  assertDigest(result.fills_sha256, dataset.fills_sha256, 'Executions');
  return result.fills;
}
export async function loadSignals(dataset: Dataset, asof: number | null): Promise<Signal[]> {
  const result = await getJson<{ signals: Signal[]; signals_sha256: string | null }>(
    `/api/signals?symbol=${encodeURIComponent(dataset.symbol)}${auditQuery(asof)}`);
  assertDigest(result.signals_sha256, dataset.signals_sha256, 'Signals');
  return result.signals;
}

/** Static research view. Without an explicit as-of cutoff it is FULL-HINDSIGHT display. */
export function makeHistoricalFeed(catalog: Catalog, asof: number | null, selectedKey: string): Datafeed {
  const symbols = new Map<string, Dataset>();
  const pairs = new Map<string, Dataset>();
  for (const ds of catalog.datasets) {
    if (datasetKey(ds) !== selectedKey) continue;
    if (!symbols.has(ds.symbol)) symbols.set(ds.symbol, ds);
    pairs.set(datasetKey(ds), ds);
  }
  return {
    async resolveSymbol(symbol: string): Promise<SymbolInfo> {
      const result = symbols.get(symbol);
      if (!result) throw new Error(`Unknown research symbol: ${symbol}`);
      return { ...result, supportedResolutions: [result.resolution] };
    },
    async searchSymbols(query: string): Promise<SymbolSearchResult[]> {
      const q = query.toLowerCase();
      return [...symbols.values()].filter(ds => ds.symbol.toLowerCase().includes(q) || ds.name.toLowerCase().includes(q));
    },
    async getBars(info: SymbolInfo, resolution: Resolution, request): Promise<BarsResult> {
      const ds = pairs.get(`${info.symbol}|${resolution}`);
      if (!ds) throw new Error(`Unregistered resolution: ${info.symbol} ${resolution}`);
      if (asof !== null && !ds.supports_asof) {
        throw new Error(`As-of replay needs available_at on EVERY bar: ${ds.symbol} ${resolution}`);
      }
      const q = new URLSearchParams({
        symbol: info.symbol, resolution,
        from: String(Math.max(0, Math.floor(request.from))),
        to: String(Math.max(0, Math.floor(request.to))),
        countBack: String(request.countBack ?? 0),
      });
      if (asof !== null) q.set('asof', String(asof));
      const result = await getJson<{ bars: Bar[]; noMoreData: boolean; csv_sha256: string }>(`/api/bars?${q}`);
      assertDigest(result.csv_sha256, ds.csv_sha256, 'OHLCV');
      return { bars: result.bars, noMoreData: result.noMoreData };
    },
    subscribeBars() {
      // Deliberately no synthetic/assumed market ticks and no browser-side execution.
      return () => {};
    },
  };
}
