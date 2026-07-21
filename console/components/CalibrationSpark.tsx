import type { CalibrationRow } from "@/lib/data";

/**
 * Compact reliability spark for registry cards: the diagonal is perfect
 * calibration, dots below it are overconfidence. No axes, area scales
 * with settled claims per bin.
 */
export default function CalibrationSpark({ bins }: { bins: CalibrationRow[] }) {
  const S = 120;
  const PAD = 10;
  const plot = S - PAD * 2;
  const x = (v: number) => PAD + v * plot;
  const y = (v: number) => S - PAD - v * plot;
  const maxN = Math.max(1, ...bins.map((b) => b.n));

  return (
    <svg
      viewBox={`0 0 ${S} ${S}`}
      role="img"
      aria-label="Calibration spark"
      className="spark"
    >
      <rect
        x={PAD}
        y={PAD}
        width={plot}
        height={plot}
        fill="none"
        stroke="#1f2736"
        strokeWidth="1"
      />
      <line
        x1={x(0)}
        y1={y(0)}
        x2={x(1)}
        y2={y(1)}
        stroke="#5eead4"
        strokeWidth="1"
        strokeDasharray="4 4"
        opacity="0.5"
      />
      {bins.map((b) => (
        <circle
          key={b.bin_low}
          cx={x(b.mean_confidence)}
          cy={y(b.frac_true)}
          r={3 + 5 * Math.sqrt(b.n / maxN)}
          fill="#5eead4"
          fillOpacity="0.3"
          stroke="#5eead4"
          strokeWidth="1"
        />
      ))}
      {bins.length === 0 && (
        <text
          x={S / 2}
          y={S / 2 + 3}
          fill="#5b6578"
          fontSize="9"
          fontFamily="monospace"
          textAnchor="middle"
        >
          no settled bins
        </text>
      )}
    </svg>
  );
}
