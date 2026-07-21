export type TapeItem = {
  mark: string;
  cls: string;
  text: string;
  conf: string;
};

export default function Tape({ items }: { items: TapeItem[] }) {
  if (items.length === 0) return null;
  const dur = Math.max(26, items.length * 2.8);
  const half = (key: string) => (
    <div className="tape-half" key={key}>
      {items.map((it, i) => (
        <div className="tape-item" key={`${key}-${i}`}>
          <span className={`tm ${it.cls}`}>{it.mark}</span>
          {it.text}
          <span className="tc"> p={it.conf}</span>
        </div>
      ))}
    </div>
  );
  return (
    <div
      className="tape"
      aria-hidden
      style={{ ["--tape-dur" as string]: `${dur}s` }}
    >
      <div className="tape-inner">
        {half("a")}
        {half("b")}
      </div>
    </div>
  );
}
