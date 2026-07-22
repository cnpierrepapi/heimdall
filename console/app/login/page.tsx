"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { createClient } from "../../lib/supabase/client";
import { EyeSigil } from "../../components/ui";
import { DemoSignIn, DEMO_EMAIL, DEMO_PASSWORD } from "../../components/DemoSignIn";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const supabase = createClient();
    const { error } = await supabase.auth.signInWithPassword({ email, password });
    if (error) {
      setError(error.message);
      setBusy(false);
      return;
    }
    router.push("/");
    router.refresh();
  }

  return (
    <main className="wrap login-wrap">
      <section className="login-card panel">
        <div className="login-brand">
          <EyeSigil />
          <span className="brand-name">Heimdall</span>
        </div>
        <h1 className="login-title">Heimdall is open</h1>
        <p className="login-sub">
          The public console needs no login at all. Signing in is only to see a private tenant
          view, scoped to one customer's own catalog by row level security. To try that as a
          judge, enter the read only demo tenant in one click.
        </p>

        <div className="login-demo">
          <DemoSignIn label="Enter the demo tenant" redirect="/" variant="solid" />
          <p className="login-demo-note">
            Or type the demo credentials below:
            <br />
            <span className="mono">{DEMO_EMAIL}</span> / <span className="mono">{DEMO_PASSWORD}</span>
          </p>
        </div>

        <div className="login-divider">
          <span>sign in manually</span>
        </div>

        <form className="login-form" onSubmit={onSubmit}>
          <label>
            <span>Email</span>
            <input
              type="email"
              autoComplete="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
            />
          </label>
          <label>
            <span>Password</span>
            <input
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </label>
          {error && <p className="login-error">{error}</p>}
          <button type="submit" disabled={busy}>
            {busy ? "Signing in..." : "Sign in"}
          </button>
        </form>

        <a className="login-back" href="/">
          <span aria-hidden="true">{"←"}</span> back to the public showcase
        </a>
      </section>
    </main>
  );
}
