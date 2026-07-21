export default function GatewayFlow({
  rogueTrust,
  floor = 55,
}: {
  rogueTrust: number | null;
  floor?: number;
}) {
  const trustLabel =
    rogueTrust !== null ? `trust ${rogueTrust.toFixed(1)}` : "trust below floor";
  const gateLabel =
    rogueTrust !== null
      ? `trust ${rogueTrust.toFixed(1)} < floor ${floor}`
      : `trust < floor ${floor}`;
  return (
    <div className="card flow">
      <div className="flow-row">
        <span className="chip">
          any MCP agent
          <small>uninstrumented is fine</small>
        </span>
        <span className="conn" />
        <span className="chip gate">
          trust gateway
          <small>claim recorded, trust checked</small>
        </span>
        <span className="conn" />
        <span className="chip ok">
          DataHub write
          <small>forwarded, now falsifiable</small>
        </span>
      </div>
      <div className="flow-row">
        <span className="chip">
          rogue-agent
          <small>{trustLabel}</small>
        </span>
        <span className="conn bad" />
        <span className="chip gate">
          policy check
          <small>{gateLabel}</small>
        </span>
        <span className="conn bad" />
        <span className="chip blocked">
          write blocked
          <small>catalog untouched</small>
        </span>
      </div>
    </div>
  );
}
