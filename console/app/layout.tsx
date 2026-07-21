import type { Metadata } from "next";
import Link from "next/link";
import { Space_Grotesk } from "next/font/google";
import "./globals.css";

const display = Space_Grotesk({
  subsets: ["latin"],
  weight: ["500", "600", "700"],
  variable: "--font-display",
  display: "swap",
});

export const metadata: Metadata = {
  title: "heimdall | computed trust for AI data agents",
  description:
    "Every agent claim is settled against reality. Brier scores, calibration, and skill-vs-luck verdicts for the agents working your DataHub catalog.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className={display.variable}>
      <body>
        <div className="shell">
          <header className="topbar">
            <Link href="/" className="brand">
              <b>heimdall</b> / trust ledger
            </Link>
            <nav className="topnav">
              <Link href="/board">Live board</Link>
              <Link href="/registry">Registry</Link>
              <Link href="/proof">Proof</Link>
              <Link href="/methodology">Methodology</Link>
              <a
                href="https://github.com/cnpierrepapi/heimdall"
                target="_blank"
                rel="noreferrer"
              >
                GitHub
              </a>
            </nav>
          </header>
          {children}
          <footer className="footer">
            <span>
              heimdall: a trust ledger for AI data agents, built on DataHub.
            </span>
            <span>
              Apache 2.0 |{" "}
              <a
                href="https://github.com/cnpierrepapi/heimdall"
                target="_blank"
                rel="noreferrer"
              >
                cnpierrepapi/heimdall
              </a>
            </span>
          </footer>
        </div>
      </body>
    </html>
  );
}
