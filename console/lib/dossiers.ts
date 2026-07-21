import { DATAHUB_URL } from "./data";

// Latest dossier document per agent in the public demo instance.
export const DOSSIER_URNS: Record<string, string> = {
  "enricher-live": "urn:li:document:shared-e4eff459-fbbb-4c40-83fc-6355260d3a17",
  "freshness-sentinel-live":
    "urn:li:document:shared-7f8fde2d-874b-496e-9a6b-c473e34975d7",
  "incident-triage-live":
    "urn:li:document:shared-755d6e23-5eeb-4c4e-b52b-3460e6d080c6",
  "blast-radius-live":
    "urn:li:document:shared-93d3a5e1-2de9-4c42-9130-db8ea5262c5e",
};

export function dossierUrl(agentId: string): string | null {
  const urn = DOSSIER_URNS[agentId];
  return urn ? `${DATAHUB_URL}/document/${encodeURIComponent(urn)}` : null;
}
