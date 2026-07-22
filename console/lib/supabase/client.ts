// Browser Supabase client, used by the login form to start a session.
import { createBrowserClient } from "@supabase/ssr";

const URL =
  process.env.NEXT_PUBLIC_SUPABASE_URL ?? "https://xfgfgcdawvfrubuczwtn.supabase.co";
const ANON = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY ?? "";

export function createClient() {
  return createBrowserClient(URL, ANON);
}
