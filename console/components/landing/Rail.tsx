"use client";

import { useEffect, useRef, useState } from "react";

export type RailStep = {
  title: string;
  body: string;
  code?: string;
};

export default function Rail({ steps }: { steps: RailStep[] }) {
  const [active, setActive] = useState(0);
  const refs = useRef<(HTMLDivElement | null)[]>([]);

  useEffect(() => {
    const io = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            setActive(Number((entry.target as HTMLElement).dataset.idx));
          }
        }
      },
      { rootMargin: "-45% 0px -45% 0px", threshold: 0 }
    );
    refs.current.forEach((el) => el && io.observe(el));
    return () => io.disconnect();
  }, []);

  return (
    <div className="rail">
      <div className="rail-left">
        <div className="rail-num">{String(active + 1).padStart(2, "0")}</div>
        <div className="rail-count">
          / {String(steps.length).padStart(2, "0")}
        </div>
      </div>
      <div className="rail-steps">
        {steps.map((s, i) => (
          <div
            key={s.title}
            data-idx={i}
            ref={(el) => {
              refs.current[i] = el;
            }}
            className={`rail-step ${i === active ? "active" : ""}`}
          >
            <h3>
              <span className="sn">{String(i + 1).padStart(2, "0")}</span>
              {s.title}
            </h3>
            <p>{s.body}</p>
            {s.code ? <pre className="rail-code">{s.code}</pre> : null}
          </div>
        ))}
      </div>
    </div>
  );
}
