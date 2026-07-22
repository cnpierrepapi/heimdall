import { Tone, verdictShort, verdictTone } from "../lib/view";

/* The trust sigil: a rune-ring gauge. Arc length = trust score, tone sets the metal. */
export function TrustRing({
  value,
  tone,
  size = 44,
}: {
  value: number | null;
  tone: Tone;
  size?: number;
}) {
  const stroke = size >= 90 ? 5 : 3;
  const r = (size - stroke - 2) / 2;
  const c = 2 * Math.PI * r;
  const v = value == null ? 0 : Math.max(0, Math.min(100, value));
  const dash = (v / 100) * c;
  return (
    <svg
      className={`ring tone-${tone}`}
      width={size}
      height={size}
      viewBox={`0 0 ${size} ${size}`}
      role="img"
      aria-label={value == null ? "not yet scored" : `trust ${Math.round(v)} of 100`}
    >
      <circle
        className="ring-track"
        cx={size / 2}
        cy={size / 2}
        r={r}
        strokeWidth={stroke}
        fill="none"
      />
      {value != null && (
        <circle
          className="ring-arc"
          cx={size / 2}
          cy={size / 2}
          r={r}
          strokeWidth={stroke}
          fill="none"
          strokeLinecap="round"
          strokeDasharray={`${dash} ${c - dash}`}
          strokeDashoffset={c / 4}
        />
      )}
      <text
        className="ring-num"
        x="50%"
        y="52%"
        textAnchor="middle"
        dominantBaseline="central"
        fontSize={size >= 90 ? size * 0.26 : size * 0.32}
      >
        {value == null ? "··" : Math.round(v)}
      </text>
    </svg>
  );
}

/* The four verdict tiers, each with its own glyph. The emotional core of the system. */
const VERDICT_GLYPH: Record<Tone, string> = {
  good: "◆", // filled diamond
  neutral: "◇", // hollow diamond
  bad: "▼", // down triangle
  none: "○", // hollow circle
};

export function VerdictChip({ verdict }: { verdict: string | null }) {
  const tone = verdictTone(verdict);
  return (
    <span className={`verdict tone-${tone}`} title={verdict ?? "unrated"}>
      <span aria-hidden="true" className="verdict-glyph">
        {VERDICT_GLYPH[tone]}
      </span>
      {verdictShort(verdict)}
    </span>
  );
}

/* Feed status marks. Blocked and held get loud chips; ok stays quiet. */
export function StatusMark({ status }: { status: string }) {
  if (status === "blocked")
    return (
      <span className="chip chip-blocked">
        <span aria-hidden="true">{"⊘"}</span> blocked
      </span>
    );
  if (status === "held")
    return (
      <span className="chip chip-held">
        <span aria-hidden="true">{"‖"}</span> held
      </span>
    );
  if (status === "error")
    return (
      <span className="chip chip-error">
        <span aria-hidden="true">{"▲"}</span> error
      </span>
    );
  return <span className="okdot" role="img" aria-label="ok" />;
}

export function OpTag({ op }: { op: string }) {
  return <span className={`op op-${op === "write" ? "write" : "read"}`}>{op}</span>;
}

/* Editorial section header: index number, title, and a right-aligned note. */
export function SectionHead({
  index,
  title,
  note,
  id,
}: {
  index: string;
  title: string;
  note?: string;
  id?: string;
}) {
  return (
    <div className="shead" id={id}>
      <span className="shead-index">{index}</span>
      <h2 className="shead-title">{title}</h2>
      {note && <span className="shead-note">{note}</span>}
    </div>
  );
}

/* The all-seeing eye. */
export function EyeSigil({ size = 26 }: { size?: number }) {
  return (
    <svg
      className="eye-sigil"
      width={size}
      height={size}
      viewBox="0 0 32 32"
      fill="none"
      aria-hidden="true"
    >
      <path
        d="M2.5 16C7 9.4 11.4 6.2 16 6.2S25 9.4 29.5 16C25 22.6 20.6 25.8 16 25.8S7 22.6 2.5 16Z"
        stroke="currentColor"
        strokeWidth="1.5"
      />
      <circle cx="16" cy="16" r="5.4" stroke="var(--aurora)" strokeWidth="1.5" />
      <circle cx="16" cy="16" r="1.9" fill="var(--aurora)" />
    </svg>
  );
}
