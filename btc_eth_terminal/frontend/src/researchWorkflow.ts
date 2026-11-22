/** Q20 strictly read-only research notebook protocol.
 * Drafts are exploratory and not trusted pre-registrations.
 * Evidence checks query the existing immutable Q7 Factor Lab research endpoint.
 * Never computes alpha, sends orders, or treats already-explored labels as OOS.
 */
export const HYPOTHESIS_FEATURES = [
  'ofi_l1_rolling_5m_base',
  'obi_l1',
  'microprice_displacement_bps',
] as const;
export type HypothesisFeature = typeof HYPOTHESIS_FEATURES[number];
export type Product = 'BTC-USD' | 'ETH-USD';
export type Expectation = 'UP' | 'DOWN';
export type Observation = {
  product: Product;
  asof: number;
  sourceSha: string;
  projectionSha: string;
};
export type HypothesisInput = {
  feature: HypothesisFeature;
  threshold: string;
  expectation: Expectation;
};
export type ResearchDraft = {
  schema: 'q20_research_draft_v1';
  status: 'EXPLORATORY_DRAFT_NOT_PREREGISTERED';
  source: {
    venue: 'COINBASE_PUBLIC_SPOT';
    mode: 'ARCHIVE_REPLAY';
    product: Product;
    asof_receiver_ms: number;
    manifest_sha256: string;
    projection_sha256: string;
  };
  hypothesis: {
    feature: HypothesisFeature;
    operator: 'GREATER_THAN_OR_EQUAL';
    threshold: string;
    expected_60s_mid_move: Expectation;
    outcome_horizon_ms: 60000;
  };
  constraints: {
    previous_archive_explored: true;
    independent_blind_oos: false;
    cost_adjusted_pnl_verified: false;
    trading_enabled: false;
    preregistration_proved: false;
  };
};
const sha = /^[a-f0-9]{64}$/;
export function makeResearchDraft(o: Observation, input: HypothesisInput): ResearchDraft {
  if (!['BTC-USD','ETH-USD'].includes(o.product) ||
      !Number.isSafeInteger(o.asof) || o.asof <= 0 ||
      !sha.test(o.sourceSha) || !sha.test(o.projectionSha)) {
    throw new Error('OBSERVATION_SOURCE_NOT_VERIFIED');
  }
  if (!HYPOTHESIS_FEATURES.includes(input.feature) ||
      !['UP','DOWN'].includes(input.expectation) ||
      typeof input.threshold !== 'string' ||
      !/^-?(?:\\d+)(?:\\.\\d{1,8})?$/.test(input.threshold.trim())) {
    throw new Error('INVALID_HYPOTHESIS');
  }
  const value = Number(input.threshold);
  const limit = input.feature === 'obi_l1' ? 1 : 1000;
  if (!Number.isFinite(value) || Math.abs(value) > limit) {
    throw new Error('HYPOTHESIS_THRESHOLD_OUT_OF_RANGE');
  }
  return {
    schema: 'q20_research_draft_v1',
    status: 'EXPLORATORY_DRAFT_NOT_PREREGISTERED',
    source: {
      venue: 'COINBASE_PUBLIC_SPOT', mode: 'ARCHIVE_REPLAY', product: o.product,
      asof_receiver_ms: o.asof, manifest_sha256: o.sourceSha,
      projection_sha256: o.projectionSha,
    },
    hypothesis: {
      feature: input.feature, operator: 'GREATER_THAN_OR_EQUAL',
      threshold: input.threshold.trim(), expected_60s_mid_move: input.expectation,
      outcome_horizon_ms: 60000,
    },
    constraints: {
      previous_archive_explored: true, independent_blind_oos: false,
      cost_adjusted_pnl_verified: false, trading_enabled: false,
      preregistration_proved: false,
    },
  };
}
export type EvidenceSummary = {
  schema: string; product: string; section: string; mode: string;
  asof_receiver_ms: number; source_sha256: string; report_sha256: string;
  verdict: string; oos_status: string; real_orders_enabled: boolean;
  primary_horizon_ms: number; predictive_effect: unknown; cost_adjusted_pnl: unknown;
  trials_executed_on_blind: number;
  summary: {
    feature_rows: number;
    primary_60s_future_mid_labels: number;
    oos_status: string;
    out_of_sample_predictive_effect: unknown;
    cost_adjusted_return_bps: unknown;
  };
};
export type GateResult = {
  verdict: 'INSUFFICIENT_EVIDENCE';
  admission: 'HISTORICAL_DESCRIPTIVE_ONLY';
  next_gate: 'UNTOUCHED_INDEPENDENT_MULTIDAY_OOS_REQUIRED';
  archived_feature_rows: number;
  historically_available_60s_labels: number;
  source_sha256: string;
  factor_report_sha256: string;
  asof_receiver_ms: number;
  oos_approved: false;
  tradeable_alpha_confirmed: false;
  orders_enabled: false;
};
/** Reject mismatched sources, future-label/PnL promotion, or a response from an old cutoff. */
export function evaluateEvidenceGate(draft: ResearchDraft, raw: unknown): GateResult {
  if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new Error('FACTOR_LAB_RESPONSE_INVALID');
  }
  const e = raw as Partial<EvidenceSummary> & Record<string, unknown>;
  const s = e.summary;
  if (e.schema !== 'q7_factor_lab_v1' || e.product !== draft.source.product ||
      e.section !== 'research' || e.mode !== 'ARCHIVE_REPLAY' ||
      e.asof_receiver_ms !== draft.source.asof_receiver_ms ||
      e.source_sha256 !== draft.source.manifest_sha256 ||
      typeof e.report_sha256 !== 'string' || !sha.test(e.report_sha256) ||
      e.verdict !== 'INSUFFICIENT_EVIDENCE' ||
      typeof e.oos_status !== 'string' || !e.oos_status.startsWith('NOT_OPENED_') ||
      e.real_orders_enabled !== false || e.primary_horizon_ms !== 60000 ||
      e.predictive_effect !== null || e.cost_adjusted_pnl !== null ||
      e.trials_executed_on_blind !== 0 ||
      !s || typeof s !== 'object' ||
      !Number.isSafeInteger(s.feature_rows) || s.feature_rows < 0 ||
      !Number.isSafeInteger(s.primary_60s_future_mid_labels) ||
      s.primary_60s_future_mid_labels < 0 ||
      s.primary_60s_future_mid_labels > s.feature_rows ||
      typeof s.oos_status !== 'string' ||
      !s.oos_status.startsWith('INSUFFICIENT_EVIDENCE') ||
      s.out_of_sample_predictive_effect !== null ||
      s.cost_adjusted_return_bps !== null ||
      Object.prototype.hasOwnProperty.call(e, 'future_only_diagnostic_labels')) {
    throw new Error('FACTOR_LAB_EVIDENCE_IDENTITY_OR_OOS_GATE_FAILURE');
  }
  return {
    verdict: 'INSUFFICIENT_EVIDENCE',
    admission: 'HISTORICAL_DESCRIPTIVE_ONLY',
    next_gate: 'UNTOUCHED_INDEPENDENT_MULTIDAY_OOS_REQUIRED',
    archived_feature_rows: s.feature_rows,
    historically_available_60s_labels: s.primary_60s_future_mid_labels,
    source_sha256: e.source_sha256!,
    factor_report_sha256: e.report_sha256!,
    asof_receiver_ms: e.asof_receiver_ms!,
    oos_approved: false, tradeable_alpha_confirmed: false, orders_enabled: false,
  };
}
export function matchingDraft(draft: ResearchDraft | null, observation: Observation): boolean {
  return draft !== null && draft.source.product === observation.product &&
    draft.source.asof_receiver_ms === observation.asof &&
    draft.source.manifest_sha256 === observation.sourceSha &&
    draft.source.projection_sha256 === observation.projectionSha;
}
