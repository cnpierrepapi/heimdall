import type { CalibrationRow } from "@/lib/data";

/**
 * Reliability diagram: stated confidence (x) against observed accuracy (y).
 * A perfectly calibrated agent sits on the diagonal; points above it are
 * underconfident, points below it are overconfident. Dot area scales with
 * the number of settled claims in the bin.
 */
export default function CalibrationChart({ bins }: { bins: CalibrationRow[] }) {
  const W = 440;
  const H = 320;
  const PAD = 44;
  const plotW = W - PAD * 2;
  const plotH = H - PAD * 2;

  const x = (v: number) => PAD + v * plotW;
  const y = (v: number) => H - PAD - v * plotH;
  const maxN = Math.max(1, ...bins.map((b) => b.n));

  const ticks = [0, 0.25, 0.5, 0.75, 1];

  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      role="img"
      aria-label="Calibration chart"
      style={{ width: "100%", height: "auto" }}
    >
      {ticks.map((t) => (
        <g key={t}>
          <line
            x1={x(t)}
            y1={y(0)}
            x2={x(t)}
            y2={y(1)}
            stroke="#1f2736"
            strokeWidth="1"
          />
          <line
            x1={x(0)}
            y1={y(t)}
            x2={x(1)}
            y2={y(t)}
            stroke="#1f2736"
            strokeWidth="1"
          />
          <text
            x={x(t)}
            y={H - PAD + 18}
            fill="#5b6578"
            fontSize="10"
            fontFamily="monospace"
            textAnchor="middle"
          >
            {t.toFixed(2)}
          </text>
          <text
            x={PAD - 10}
            y={y(t) + 3}
            fill="#5b6578"
            fontSize="10"
            fontFamily="monospace"
            textAnchor="end"
          >
            {t.toFixed(2)}
          </text>
        </g>
      ))}

      <line
        x1={x(0)}
        y1={y(0)}
        x2={x(1)}
        y2={y(1)}
        stroke="#5eead4"
        strokeWidth="1"
        strokeDasharray="5 5"
        opacity="0.5"
      />

      {bins.map((b) => (
        <circle
          key={b.bin_low}
          cx={x(b.mean_confidence)}
          cy={y(b.frac_true)}
          r={6 + 10 * Math.sqrt(b.n / maxN)}
          fill="#5eead4"
          fillOpacity="0.25"
          stroke="#5eead4"
          strokeWidth="1.5"
        />
      ))}

      <text
        x={W / 2}
        y={H - 8}
        fill="#8b96ab"
        fontSize="11"
        textAnchor="middle"
      >
        stated confidence
      </text>
      <text
        x={14}
        y={H / 2}
        fill="#8b96ab"
        fontSize="11"
        textAnchor="middle"
        transform={`rotate(-90 14 ${H / 2})`}
      >
        observed accuracy
      </text>
    </svg>
  );
}
