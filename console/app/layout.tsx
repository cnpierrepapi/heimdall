import type { Metadata } from "next";
import { Fraunces, JetBrains_Mono } from "next/font/google";
import { EyeSigil } from "../components/ui";
import { getViewer } from "../lib/data";
import "./globals.css";

const display = Fraunces({
  subsets: ["latin"],
  style: ["normal", "italic"],
  variable: "--font-display",
});
const mono = JetBrains_Mono({
  subsets: ["latin"],
  variable: "--font-mono",
});

export const metadata: Metadata = {
  metadataBase: new URL("https://heimdall-tech.vercel.app"),
  title: "Heimdall · the watch on your catalog",
  description:
    "Heimdall watches every AI agent acting on your DataHub catalog. It observes each action, grounds it in catalog context, scores skill against luck, governs writes in flight, and writes trust back into the catalog.",
};

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  const viewer = await getViewer();
  return (
    <html lang="en" className={`${display.variable} ${mono.variable}`}>
      <body>
        <div className="aurora" aria-hidden="true" />
        <header className="topbar">
          <div className="wrap topbar-in">
            <a className="brand" href="/">
              <EyeSigil />
              <span className="brand-name">Heimdall</span>
            </a>
            <nav className="topnav" aria-label="Sections">
              <a href="/judge">For judges</a>
              <a href="/#activity">Activity</a>
              <a href="/#leaderboard">Leaderboard</a>
              <a href="/#findings">Findings</a>
              <a
                className="topnav-ext"
                href="https://datahub.onenept.com"
                target="_blank"
                rel="noreferrer"
              >
                DataHub <span aria-hidden="true">{"↗"}</span>
              </a>
              {viewer.email ? (
                <span className="authbox">
                  <span className="authbox-who">
                    <span className="mono">{viewer.owner}</span>
                  </span>
                  <form action="/auth/signout" method="post">
                    <button type="submit" className="authbox-btn">
                      Sign out
                    </button>
                  </form>
                </span>
              ) : (
                <a className="authbox-signin" href="/login">
                  Sign in
                </a>
              )}
            </nav>
          </div>
          <div className="bifrost-line" aria-hidden="true" />
        </header>
        {children}
        <footer className="site-footer">
          <div className="wrap footer-in">
            <div className="footer-brand">
              <EyeSigil size={20} />
              <span>Heimdall</span>
            </div>
            <p>
              Agent observability and trust control plane for DataHub. Everything on this page
              is live from the showcase catalog <span className="mono">lineworld</span>, public
              data, read only. The catalog itself is at{" "}
              <a href="https://datahub.onenept.com" target="_blank" rel="noreferrer">
                datahub.onenept.com
              </a>
              .
            </p>
          </div>
        </footer>
      </body>
    </html>
  );
}
