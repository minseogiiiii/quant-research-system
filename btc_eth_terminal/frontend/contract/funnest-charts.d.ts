/** Narrow subset copied from upstream source packages/core/src/types.ts (v0.6.24).
 * Used ONLY for a networkless adapter contract check, NOT a substitute for npm types.
 */
declare module '@funnest/charts' {
  export interface Bar { time: number; open: number; high: number; low: number; close: number; volume?: number }
  export type Resolution = string;
  export type TickSize = number | ((price: number) => number);
  export type SymbolType = 'stock' | 'etf' | 'index' | 'crypto' | 'futures' | 'forex';
  export interface SymbolInfo {
    symbol: string; name: string; exchange: string; timezone: string;
    session: string; pricePrecision: number; tickSize: TickSize;
    volumePrecision?: number; logoUrl?: string; type?: SymbolType; country?: string;
    supportedResolutions?: Resolution[];
  }
  export interface SymbolSearchResult extends SymbolInfo {
    price?: number; changePercent?: number; sparkline?: number[];
  }
  export interface BarsRequest { from: number; to: number; countBack?: number }
  export interface BarsResult { bars: Bar[]; noMoreData: boolean }
  export type Unsubscribe = () => void;
  export interface ConnectionState { status: 'connecting' | 'connected' | 'reconnecting' | 'disconnected'; latency?: number }
  export interface Quote { price?: number; change?: number; changePercent?: number }
  export interface Datafeed {
    resolveSymbol(symbol: string): Promise<SymbolInfo>;
    getBars(symbol: SymbolInfo, resolution: Resolution, request: BarsRequest): Promise<BarsResult>;
    subscribeBars(symbol: SymbolInfo, resolution: Resolution, onBar: (bar: Bar) => void,
      onResetCache?: () => void, onConnection?: (state: ConnectionState) => void): Unsubscribe;
    searchSymbols?(query: string): Promise<SymbolSearchResult[]>;
    searchSymbolsProgressive?(query: string, onPartial: (results: SymbolSearchResult[]) => void): Promise<SymbolSearchResult[]>;
    getSparkline?(symbol: string): Promise<number[]>;
    subscribeQuotes?(symbol: SymbolInfo, onQuote: (quote: Quote) => void): Unsubscribe;
  }
}
