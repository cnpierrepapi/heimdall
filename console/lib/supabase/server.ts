// Server-side Supabase client, bound to the request cookies so RLS runs as the
// signed-in user (or anon when there is no session).
import { createServerClient, type CookieOptions } from "@supabase/ssr";
import { cookies } from "next/headers";

type CookieToSet = { name: string; value: string; options: CookieOptions };

const URL =
  process.env.NEXT_PUBLIC_SUPABASE_URL ?? "https://xfgfgcdawvfrubuczwtn.supabase.co";
const ANON = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY ?? "";

export async function createClient() {
  const cookieStore = await cookies();
  return createServerClient(URL, ANON, {
    cookies: {
      getAll() {
        return cookieStore.getAll();
      },
      setAll(cookiesToSet: CookieToSet[]) {
        try {
          cookiesToSet.forEach(({ name, value, options }) =>
            cookieStore.set({ name, value, ...options })
          );
        } catch {
          // called from a Server Component; the middleware refreshes cookies.
        }
      },
    },
  });
}
