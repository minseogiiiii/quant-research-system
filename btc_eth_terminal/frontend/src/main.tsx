import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { FnChart } from '@funnest/charts-react';
import type { TradingChart } from '@funnest/charts-ui';
import {
  datasetKey, loadCatalog, loadFills, loadSignals, makeHistoricalFeed,
  type Catalog, type Dataset, type Fill, type Signal,
} from './datafeed';
import './style.css';
import UnifiedTerminal from './unified';

function formatMs(t: number) { return new Date(t).toISOString().replace('.000Z', 'Z'); }
function errString(e: unknown): string { return e instanceof Error ? e.message : String(e); }
function isBtcEth(ds: Dataset): boolean {
  return ds.type === 'crypto' && ds.resolution === '5' && /^(BTC|ETH)/i.test(ds.symbol);
}

function ResearchChart({ ds, catalog, asof, onError }: {
  ds: Dataset; catalog: Catalog; asof: number | null; onError: (error: string) => void;
}) {
  const chartRef = useRef<TradingChart | null>(null);
  const feed = useMemo(() => makeHistoricalFeed(catalog, asof, datasetKey(ds)), [catalog, asof]);
  const [signals, setSignals] = useState<Signal[]>([]);
  const [fills, setFills] = useState<Fill[]>([]);
  const [loaded, setLoaded] = useState(false);

  // Initial chart readiness and later symbol changes are both supported.
  // Previously we only loaded executions on symbolChange, which may have
  // already fired before onReady in some chart lifecycles.
  const onReady = useCallback((widget: TradingChart) => {
    chartRef.current = widget;
    let requestSeq = 0;
    const refreshExecutions = () => {
      const seq = ++requestSeq;
      setLoaded(false);
      void loadFills(ds, asof).then((events) => {
        if (seq !== requestSeq || chartRef.current !== widget) return;
        setFills(events);
        widget.chart.setExecutions(events.map(e => ({
          time: e.time, price: e.price, side: e.side, quantity: e.quantity,
        })));
        setLoaded(true);
      }).catch((e: unknown) => {
        if (seq === requestSeq && chartRef.current === widget) onError(errString(e));
      });
    };
    // If FnChart has already emitted its initial symbolChange, we still
    // explicitly load the immutable execution ledger once.
    refreshExecutions();
    const unsubscribe = widget.chart.on('symbolChange', (info) => {
      if (info.symbol === ds.symbol) refreshExecutions();
    });
    // FnChart destroys subscriptions with the chart instance. We retain this
    // reference so the application cannot accidentally register duplicates.
    void unsubscribe;
  }, [ds, asof, onError]);

  useEffect(() => {
    let active = true;
    setSignals([]);
    setFills([]);
    void loadSignals(ds, asof).then(v => { if (active) setSignals(v); })
      .catch((e: unknown) => { if (active) onError(errString(e)); });
    return () => { active = false; chartRef.current = null; };
  }, [ds, asof, onError]);

  return <>
    <div className="data-info">
      <span>Source <strong>{ds.source_type.toUpperCase()}</strong></span>
      <span>Evidence label <strong>{ds.evidence_status.replaceAll('_', ' ')} (declared)</strong></span>
      <span>Venue <strong>{ds.exchange}</strong></span>
      <span>Candles <strong>{ds.row_count.toLocaleString()}</strong></span>
      <span>UTC coverage <strong>{formatMs(ds.earliest_time)} to {formatMs(ds.latest_time)}</strong></span>
      <span>CSV SHA <code title={ds.csv_sha256}>{ds.csv_sha256.slice(0, 16)}…</code></span>
      {ds.original_raw_sha256 && <span>Source SHA <code title={ds.original_raw_sha256}>{ds.original_raw_sha256.slice(0, 16)}…</code></span>}
    </div>
    <p className="minor">{asof === null ? 'FULL-HINDSIGHT VIEW: all history may be visible, not a valid trading-time information set.'
      : `DECLARED-AVAILABILITY FILTER: only bars/events with recorded availability ≤ ${formatMs(asof)} are returned. Origin of availability timestamps is NOT independently verified.`}</p>
    <div className="chart">
      <FnChart key={`${datasetKey(ds)}:${asof ?? 'all'}`} datafeed={feed} symbol={ds.symbol}
        resolution={ds.resolution} locale="en" colorScheme="dark" storageKey={null}
        drawingToolbar={true} onReady={onReady} style={{ height: '100%' }} />
    </div>
    <div className="table-zone">
      <div className="table-head"><h3>Candidate decisions</h3><span>Never treated as fills</span></div>
      {signals.length ? <div className="overflow"><table><thead><tr>
        <th>Decision (UTC)</th><th>Model</th><th>Side</th><th>Available at (UTC)</th>
      </tr></thead><tbody>{signals.slice(-16).map(s => <tr key={s.signal_id}>
        <td>{formatMs(s.time)}</td><td>{s.model_id}</td><td><b>{s.side}</b></td><td>{formatMs(s.available_at)}</td>
      </tr>)}</tbody></table></div> : <p className="muted">No candidate signal file configured, or no admissible decisions at the selected cutoff.</p>}
      {signals.length > 16 && <p className="minor">Showing last 16 of {signals.length} candidates; no OOS performance inferred.</p>}
      <div className="table-head"><h3>Recorded executions</h3><span>Read-only visualization</span></div>
      {fills.length ? <p className="minor">{fills.length} recorded fills: {Array.from(new Set(fills.map(f => f.event_type))).join(', ')}. Provenance must be checked against the independent backtest or account event ledger.</p>
        : <p className="muted">No recorded fills attached. A LONG or SHORT candidate is not an execution.</p>}
      {!loaded && <p className="minor">Chart data is loading or blocked by an evidence integrity check.</p>}
    </div>
  </>;
}

function App() {
  // Keep legacy CSV research in a separate, deliberately opened workspace.
  // Its historical source lineage differs from the audited v30 L2 archive.
  const [legacyOpen,setLegacyOpen]=useState(false);
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [selection, setSelection] = useState('');
  const [error, setError] = useState('');
  const [chartBlocked, setChartBlocked] = useState(false);
  const [cutoffInput, setCutoffInput] = useState('');
  const [asof, setAsof] = useState<number | null>(null);
  const onError = useCallback((message: string) => {
    setError(message);
    // Never leave potentially stale price bars or recorded fills visible after
    // source verification or feed failure. Re-audit catalog on full reload.
    setChartBlocked(true);
  }, []);

  useEffect(() => {
    if(!legacyOpen)return;
    let live = true;
    void loadCatalog().then(d => {
      if (!live) return;
      setCatalog(d);
      const first = d.datasets.find(isBtcEth);
      if (first) setSelection(datasetKey(first));
    }).catch((e: unknown) => { if (live) setError(errString(e)); });
    return () => { live = false; };
  }, [legacyOpen]);
  const datasets = (catalog?.datasets ?? []).filter(isBtcEth);
  const ds = datasets.find(d => datasetKey(d) === selection) ?? null;

  const applyCutoff = () => {
    const value = cutoffInput.trim();
    if (!value) { setAsof(null); setError(''); return; }
    if (!/(?:Z|[+-]\d{2}:\d{2})$/i.test(value)) {
      setError('As-of timestamp must have an explicit UTC offset, e.g. 2026-11-02T14:32:00Z.');
      return;
    }
    const ms = Date.parse(value);
    if (!Number.isFinite(ms) || ms < 631152000000 || ms > 7258118400000) {
      setError('Invalid as-of timestamp.');
      return;
    }
    if (ds && !ds.supports_asof) {
      setError('This dataset lacks per-bar available_at. As-of replay is blocked, not guessed.');
      return;
    }
    setError('');
    setAsof(ms);
  };

  return <main>
    <header><div><h1>BTC / ETH Research Terminal</h1><p>Coinbase spot 5-minute data integrity pilot · Funnest Charts</p></div>
      <span className="badge">RESEARCH ONLY · NO ORDERS</span></header>
    <UnifiedTerminal />
    <section className="legacy-workspace-toggle" aria-label="Isolated legacy research CSV workspace">
      <h2>Separate historical CSV sandbox</h2>
      <p>The older five-minute CSV datasets are from a distinct historical source and are NOT interchangeable with the audited Coinbase L2 archive above. Do not merge their observations, time windows, labels or performance.</p>
      <button type="button" aria-expanded={legacyOpen} onClick={()=>setLegacyOpen(v=>!v)}>{legacyOpen?'Close separate CSV sandbox':'Open separate CSV sandbox'}</button>
    </section>
    {legacyOpen ? <section className="legacy-sandbox">
      <div className="legacy-source-warning" role="note">ISOLATED SOURCE · DECLARED AVAILABILITY ONLY · NOT PART OF THE AUDITED L2 COHORT · NO JOINT OOS OR PNL</div>
    {error && <div className="error" role="alert">{error}{chartBlocked ? ' Price display blocked until the data source is re-audited and the page reloaded.' : ''}</div>}
    <section className="research"><h2>BTC / ETH hypothesis status</h2>
      <div className="cards">{(catalog?.research ?? []).filter(r => r.market === 'BTC/ETH').map(r => <article key={r.id}>
        <small>{r.id} · {r.market}</small>
        <h3>{r.status.replaceAll('_', ' ')}</h3>
        <p>{r.description}</p>
        <p className="minor">Executable alpha: <strong>{r.validated_executable_alpha ? 'Ledger claims yes; independent verification required' : 'NOT CONFIRMED'}</strong></p>
        {r.next_gate && <p className="muted">Next evidence gate: {r.next_gate}</p>}
        {r.ledger_as_of && <p className="minor">Ledger dated {r.ledger_as_of}; not live-synced · SHA {r.ledger_sha256?.slice(0, 12)}…</p>}
        {r.report_as_of && <p className="minor">Research report dated {r.report_as_of} · SHA {r.report_sha256?.slice(0, 12)}…</p>}
      </article>)}</div>
    </section>
    <section className="panel"><div className="panel-head">
      <div><h2>BTC-USD / ETH-USD · 5-minute OHLCV</h2><p>Price bars only; no order book, no venue execution simulation. No chart without a real recorded CSV.</p></div>
      <select aria-label="Dataset" value={selection} disabled={chartBlocked} onChange={e => { setSelection(e.target.value); setAsof(null); setCutoffInput(''); setError(''); }}>
        {!datasets.length && <option value="">No BTC/ETH datasets connected</option>}
        {datasets.map(d => <option value={datasetKey(d)} key={datasetKey(d)}>{d.symbol} · {d.resolution}m · {d.exchange}</option>)}
      </select>
    </div>
    {ds && catalog ? <>
      <div className="cutoff">
        <div><label htmlFor="cutoff">Declared-availability cutoff (ISO 8601 with explicit timezone)</label>
          <input id="cutoff" type="text" placeholder={ds.supports_asof ? '2026-11-02T14:32:00Z' : 'Unavailable for retroactive historical snapshots'} disabled={!ds.supports_asof} value={cutoffInput} onChange={e => setCutoffInput(e.target.value)}/></div>
        <button type="button" disabled={!ds.supports_asof} onClick={applyCutoff}>Apply declared-availability cutoff</button>
      </div>
      {!ds.supports_asof && <p className="minor">Historical API candles have bar-open times, not historical receive timestamps. An as-of trading-time view is unavailable without independently observed arrival data.</p>}
      {chartBlocked ? <div className="empty" role="alert">DATA INTEGRITY GATE CLOSED. No historical bars, candidate signals or execution markers will be displayed until a fresh catalog audit succeeds after reloading.</div>
        : <ResearchChart key={`${selection}:${asof ?? 'all'}`} ds={ds} catalog={catalog} asof={asof} onError={onError}/>}
    </> : <div className="empty">No BTC/ETH five-minute OHLCV files have been registered. Run the read-only two-market Coinbase collector, or import existing BTC/ETH feature files after auditing their timestamp columns. No synthetic market prices will be displayed.</div>}
    </section>
    </section> : null}
    <footer>BTC and ETH only. Coinbase BTC-USD / ETH-USD spot are NOT Binance BTCUSDT / ETHUSDT. Market charting does not prove a strategy is profitable. No live orders, account credentials or future labels are connected.</footer>
  </main>;
}
createRoot(document.getElementById('root')!).render(<React.StrictMode><App /></React.StrictMode>);
