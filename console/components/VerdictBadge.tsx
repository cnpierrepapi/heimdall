const CLASS_BY_VERDICT: Record<string, string> = {
  skilled: "skilled",
  "not distinguishable from luck": "luck",
  "worse than chance": "harmful",
  "insufficient settled claims": "unsettled",
};

const SHORT_BY_VERDICT: Record<string, string> = {
  skilled: "skilled",
  "not distinguishable from luck": "luck, not skill",
  "worse than chance": "worse than chance",
  "insufficient settled claims": "unproven",
};

export function verdictClass(verdict: string): string {
  return CLASS_BY_VERDICT[verdict] ?? "unsettled";
}

export default function VerdictBadge({
  verdict,
  long = false,
}: {
  verdict: string;
  long?: boolean;
}) {
  return (
    <span className={`badge ${verdictClass(verdict)}`}>
      {long ? verdict : SHORT_BY_VERDICT[verdict] ?? verdict}
    </span>
  );
}
