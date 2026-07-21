// Read-only projection of the public Heimdall tables (anon key, RLS-guarded).
const SUPABASE_URL =
  process.env.NEXT_PUBLIC_SUPABASE_URL ?? "https://xfgfgcdawvfrubuczwtn.supabase.co";
const ANON = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY ?? "";
export const DATAHUB_URL =
  process.env.NEXT_PUBLIC_DATAHUB_URL ?? "https://datahub.onenept.com";

async function q<T>(path: string): Promise<T[]> {
  try {
    const res = await fetch(`${SUPABASE_URL}/rest/v1/${path}`, {
      headers: { apikey: ANON, Authorization: `Bearer ${ANON}` },
      next: { revalidate: 30 },
    });
    if (!res.ok) return [];
    return (await res.json()) as T[];
  } catch {
    return [];
  }
}

export type AgentRow = {
  agent_id: string;
  work_kind: string;
  trust: number | null;
  verdict: string | null;
  n_settled: number | null;
  brier: number | null;
  win_rate: number | null;
  visibility: string | null;
  owner: string | null;
  catalog: string | null;
};

export type ActivityRow = {
  id: number;
  agent_id: string;
  tool: string;
  op: string;
  status: string;
  entities: string[] | null;
  latency_ms: number | null;
  result_summary: string | null;
  ts: string;
};

export type FindingRow = {
  id: number;
  agent_id: string;
  check_type: string;
  severity: string;
  verdict: string | null;
  entity_urn: string | null;
  column: string | null;
  reason: string | null;
  ts: string;
};

export const getAgents = () => q<AgentRow>("hd_agents?order=trust.desc");
export const getActivity = () =>
  q<ActivityRow>("hd_activity?owner=eq.showcase&order=ts.desc&limit=40");
export const getFindings = () =>
  q<FindingRow>("hd_findings?owner=eq.showcase&order=ts.desc");

export const WORK_KIND_LABEL: Record<string, string> = {
  column_doc: "Column documentation",
  table_doc: "Table documentation",
  pii: "PII tagging",
  owner: "Ownership",
  domain: "Domain assignment",
  term: "Glossary terms",
};

// dataset urn -> short name and a DataHub deep link
export function datasetName(urn: string): string {
  const m = urn.match(/,([^,]+),PROD\)/);
  return m ? m[1] : urn;
}
export function datahubLink(urn: string): string {
  return `${DATAHUB_URL}/dataset/${encodeURIComponent(urn)}/`;
}

export function verdictTone(verdict: string | null): "good" | "bad" | "neutral" {
  if (verdict === "skilled") return "good";
  if (verdict === "worse than chance") return "bad";
  return "neutral";
}
