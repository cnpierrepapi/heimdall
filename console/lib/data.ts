const SUPABASE_URL = process.env.NEXT_PUBLIC_SUPABASE_URL ?? "";
const ANON_KEY = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY ?? "";

export const DATAHUB_URL =
  process.env.NEXT_PUBLIC_DATAHUB_URL ?? "https://datahub.onenept.com";

export type AgentRow = {
  agent_id: string;
  model_id: string | null;
  trust: number;
  verdict: string;
  n_total: number;
  n_settled: number;
  wins: number | null;
  win_rate: number | null;
  brier_mean: number | null;
  ece: number | null;
  p_value: number | null;
  q_value: number | null;
  expected_null_wins: number | null;
  updated_at: string;
};

export type CalibrationRow = {
  agent_id: string;
  bin_low: number;
  bin_high: number;
  n: number;
  mean_confidence: number;
  frac_true: number;
};

export type ClaimRow = {
  claim_id: string;
  agent_id: string;
  model_id: string | null;
  claim_type: string;
  entity_urn: string;
  prediction: Record<string, unknown>;
  confidence: number;
  created_ts: number;
  settled_ts: number | null;
  correct: boolean | null;
};

async function rest<T>(path: string): Promise<T[]> {
  if (!SUPABASE_URL || !ANON_KEY) {
    // unconfigured build environment: render the empty state rather than
    // failing the build; production always has these set
    return [];
  }
  const res = await fetch(`${SUPABASE_URL}/rest/v1/${path}`, {
    headers: { apikey: ANON_KEY, Authorization: `Bearer ${ANON_KEY}` },
    next: { revalidate: 60 },
  });
  if (!res.ok) {
    throw new Error(`scoreboard data fetch failed: ${res.status} ${path}`);
  }
  return res.json();
}

export async function getAgents(): Promise<AgentRow[]> {
  return rest<AgentRow>("ll_agents?select=*&order=trust.desc");
}

export async function getAgent(agentId: string): Promise<AgentRow | null> {
  const rows = await rest<AgentRow>(
    `ll_agents?select=*&agent_id=eq.${encodeURIComponent(agentId)}`
  );
  return rows[0] ?? null;
}

export async function getCalibration(agentId: string): Promise<CalibrationRow[]> {
  return rest<CalibrationRow>(
    `ll_calibration?select=*&agent_id=eq.${encodeURIComponent(agentId)}&order=bin_low.asc`
  );
}

export async function getClaims(agentId: string): Promise<ClaimRow[]> {
  return rest<ClaimRow>(
    `ll_claims?select=*&agent_id=eq.${encodeURIComponent(agentId)}&order=created_ts.desc&limit=100`
  );
}

export async function getRecentClaims(limit = 24): Promise<ClaimRow[]> {
  return rest<ClaimRow>(
    `ll_claims?select=*&order=created_ts.desc&limit=${limit}`
  );
}

export function datasetName(urn: string): string {
  const parts = urn.split(",");
  return parts.length > 1 ? parts[1] : urn;
}

export function describeClaim(
  claimType: string,
  prediction: Record<string, unknown>,
  entityUrn: string
): string {
  const ds = datasetName(entityUrn);
  if (claimType === "blast_radius") {
    return `${prediction["will_break"] ? "break" : "survive"}: ${ds} after dropping ${prediction["dropped_column"]}`;
  }
  if (claimType === "freshness_sla") {
    return `${prediction["will_miss_sla"] ? "miss" : "hit"} SLA: ${ds} (day ${prediction["day"]})`;
  }
  if (claimType === "root_cause") {
    return `root cause of ${ds}: ${datasetName(String(prediction["root_cause_urn"] ?? ""))} (1 of ${prediction["n_candidates"]})`;
  }
  if (claimType === "enrichment") {
    const implicit = prediction["implicit"] ? " (implicit, via gateway)" : "";
    return `document ${ds}.${prediction["column"]}${implicit}`;
  }
  return `${claimType} on ${ds}`;
}
