// Session-aware projection of the Heimdall tables. Reads run through the
// per-request Supabase client so RLS scopes rows to the signed-in tenant, or
// to the public showcase when there is no session. This module is server only
// (it touches request cookies); the client-safe helpers live in view.ts and
// are re-exported here so existing imports keep working.
import { createClient } from "./supabase/server";
import type { AgentRow, ActivityRow, FindingRow, Viewer } from "./view";

export * from "./view";

export async function getViewer(): Promise<Viewer> {
  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();
  if (!user) return { email: null, owner: "showcase", isTenant: false };
  const { data } = await supabase
    .from("hd_members")
    .select("owner")
    .order("owner")
    .limit(1);
  const owner = data && data[0]?.owner ? (data[0].owner as string) : "showcase";
  return { email: user.email ?? null, owner, isTenant: owner !== "showcase" };
}

// The leaderboard is global: RLS returns every public agent plus any private
// agents the viewer's tenant owns.
export async function getAgents(): Promise<AgentRow[]> {
  const supabase = await createClient();
  const { data } = await supabase
    .from("hd_agents")
    .select("*")
    .order("trust", { ascending: false, nullsFirst: false });
  return (data as AgentRow[] | null) ?? [];
}

export async function getActivity(owner = "showcase"): Promise<ActivityRow[]> {
  const supabase = await createClient();
  const { data } = await supabase
    .from("hd_activity")
    .select("*")
    .eq("owner", owner)
    .order("ts", { ascending: false })
    .limit(40);
  return (data as ActivityRow[] | null) ?? [];
}

export async function getFindings(owner = "showcase"): Promise<FindingRow[]> {
  const supabase = await createClient();
  const { data } = await supabase
    .from("hd_findings")
    .select("*")
    .eq("owner", owner)
    .order("ts", { ascending: false });
  return (data as FindingRow[] | null) ?? [];
}
