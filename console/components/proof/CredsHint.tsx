"use client";

import { useState } from "react";

export default function CredsHint() {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText("ledger-judge-2026");
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // clipboard unavailable: the credentials are visible as text anyway
    }
  }

  return (
    <div className="creds-hint">
      links open the live instance. sign in once as <b>judge</b> /{" "}
      <b>ledger-judge-2026</b>, DataHub then drops you on the linked page.
      <button type="button" onClick={copy} className="copy-btn">
        {copied ? "password copied" : "copy password"}
      </button>
    </div>
  );
}
