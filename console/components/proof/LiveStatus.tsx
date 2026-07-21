"use client";

import { useEffect, useState } from "react";

type Status = {
  up: boolean;
  status: number;
  ms: number;
  checkedAt: string;
};

export default function LiveStatus() {
  const [status, setStatus] = useState<Status | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function probe() {
      try {
        const res = await fetch("/api/dh-status", { cache: "no-store" });
        const data = (await res.json()) as Status;
        if (!cancelled) setStatus(data);
      } catch {
        if (!cancelled) setStatus({ up: false, status: 0, ms: 0, checkedAt: "" });
      }
    }
    probe();
    const timer = setInterval(probe, 30000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  if (status === null) {
    return (
      <span className="status-pill">
        <span className="dot wait" />
        checking demo instance...
      </span>
    );
  }
  return status.up ? (
    <span className="status-pill up">
      <span className="dot" />
      demo DataHub live now ({status.ms}ms)
    </span>
  ) : (
    <span className="status-pill down">
      <span className="dot" />
      demo DataHub unreachable, screenshots below stand in
    </span>
  );
}
