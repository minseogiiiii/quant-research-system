/** Fail-closed browser admission for read-only BTC/ETH source display.
 * Use monotonic elapsed time rather than archived Coinbase receiver timestamps.
 * An HTTP request that never finishes must not preserve an old HEALTHY quote.
 */
export const HEALTH_PROBE_MAX_AGE_MS = 10_000;
export const READ_ONLY_API_TIMEOUT_MS = 8_000;

type SourceHealth = {status:string;mode:string;live_feed:boolean;trading_enabled:boolean};

export function freshSourceHealth(
  health:SourceHealth|null, lastSuccessfulProbeMs:number|null, nowMs:number,
):boolean {
  if(!health||health.status!=='HEALTHY'||health.trading_enabled!==false ||
     !(health.mode==='LIVE_SOURCE'&&health.live_feed===true ||
       health.mode==='ARCHIVE_SIMULATION'&&health.live_feed===false))return false;
  return lastSuccessfulProbeMs!==null&&Number.isFinite(lastSuccessfulProbeMs)&&
    Number.isFinite(nowMs)&&lastSuccessfulProbeMs>=0&&nowMs>=lastSuccessfulProbeMs&&
    nowMs-lastSuccessfulProbeMs<=HEALTH_PROBE_MAX_AGE_MS;
}

/** Abort even if fetch stalls before HTTP headers. Caller abort still wins. */
export async function readOnlyJson<T>(
  url:string, parentSignal?:AbortSignal, timeoutMs=READ_ONLY_API_TIMEOUT_MS,
):Promise<T> {
  if(!Number.isFinite(timeoutMs)||timeoutMs<=0)throw new Error('INVALID_HTTP_TIMEOUT');
  const timeout=AbortSignal.timeout(timeoutMs);
  const signal=parentSignal?AbortSignal.any([parentSignal,timeout]):timeout;
  const response=await fetch(url,{cache:'no-store',signal});
  if(!response.ok)throw new Error(`${url} HTTP ${response.status}`);
  return await response.json() as T;
}
