// Client-safe view helpers, types, and labels. No server imports live here, so
// both Server Components and Client Components (and components/ui.tsx) can use
// it. The session-aware data fetchers live in data.ts.

export const DATAHUB_URL =
  process.env.NEXT_PUBLIC_DATAHUB_URL ?? "https://datahub.onenept.com";

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

// Who is viewing: the signed-in email and the tenant (owner) their session can
// see. Anonymous visitors get the public showcase tenant.
export type Viewer = { email: string | null; owner: string; isTenant: boolean };

export const WORK_KIND_LABEL: Record<string, string> = {
  column_doc: "Column documentation",
  table_doc: "Table documentation",
  pii: "PII tagging",
  owner: "Ownership",
  domain: "Domain assignment",
  term: "Glossary terms",
};

export const CHECK_LABEL: Record<string, string> = {
  glossary_conflict: "Glossary conflict",
  pii_scope: "PII scope",
  undefined_column: "Undefined column",
  low_quality_description: "Low quality description",
  wrong_owner: "Wrong owner",
  wrong_domain: "Wrong domain",
};

// dataset urn -> short name and a DataHub deep link
export function datasetName(urn: string): string {
  const m = urn.match(/,([^,]+),PROD\)/);
  return m ? m[1] : urn;
}
export function datahubLink(urn: string): string {
  return `${DATAHUB_URL}/dataset/${encodeURIComponent(urn)}/`;
}

export type Tone = "good" | "neutral" | "bad" | "none";

export function verdictTone(verdict: string | null): Tone {
  if (verdict === "skilled") return "good";
  if (verdict === "worse than chance") return "bad";
  if (verdict === "insufficient settled claims" || verdict == null) return "none";
  return "neutral";
}

export function verdictShort(verdict: string | null): string {
  if (verdict === "skilled") return "skilled";
  if (verdict === "worse than chance") return "worse than chance";
  if (verdict === "not distinguishable from luck") return "luck range";
  if (verdict === "insufficient settled claims") return "insufficient data";
  return "unrated";
}

export function relativeTime(ts: string): string {
  const d = (Date.now() - new Date(ts).getTime()) / 1000;
  if (d < 90) return `${Math.max(1, Math.round(d))}s ago`;
  if (d < 5400) return `${Math.round(d / 60)}m ago`;
  if (d < 129600) return `${Math.round(d / 3600)}h ago`;
  return `${Math.round(d / 86400)}d ago`;
}
