"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { createClient } from "../lib/supabase/client";

// The demo tenant is a public, read-only account over fake `acme` data. It is
// meant to be open: any judge can enter the tenant view in one click, no typing.
export const DEMO_EMAIL = "acme-demo@heimdall.tech";
export const DEMO_PASSWORD = "heimdall-demo";

export function DemoSignIn({
  label = "Enter the demo tenant",
  redirect = "/",
  variant = "solid",
}: {
  label?: string;
  redirect?: string;
  variant?: "solid" | "ghost";
}) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function go() {
    setBusy(true);
    setError(null);
    const supabase = createClient();
    const { error } = await supabase.auth.signInWithPassword({
      email: DEMO_EMAIL,
      password: DEMO_PASSWORD,
    });
    if (error) {
      setError(error.message);
      setBusy(false);
      return;
    }
    router.push(redirect);
    router.refresh();
  }

  return (
    <span className="demo-signin">
      <button
        type="button"
        onClick={go}
        disabled={busy}
        className={`cta ${variant === "solid" ? "cta-solid" : "cta-ghost"}`}
      >
        {busy ? "Entering..." : label}
      </button>
      {error && <span className="demo-error">{error}</span>}
    </span>
  );
}
