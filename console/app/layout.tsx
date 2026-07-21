import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Heimdall — Agent Observability & Trust for DataHub",
  description:
    "Heimdall watches every AI agent acting on your DataHub catalog: it observes each action, grounds it in catalog context, scores agent reliability, and governs writes in flight.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <div className="wrap">
          <header className="topbar">
            <a className="brand" href="/">
              <span className="eye" /> heimdall
            </a>
            <nav>
              <a href="/#leaderboard">Leaderboard</a>
              <a href="/#activity">Activity</a>
              <a href="/#findings">Findings</a>
              <a href="https://datahub.onenept.com" target="_blank" rel="noreferrer">
                DataHub
              </a>
            </nav>
          </header>
        </div>
        {children}
      </body>
    </html>
  );
}
