import { DATAHUB_URL } from "@/lib/data";

export const dynamic = "force-dynamic";

export async function GET() {
  const started = Date.now();
  try {
    const res = await fetch(`${DATAHUB_URL}/`, {
      cache: "no-store",
      redirect: "follow",
      signal: AbortSignal.timeout(6000),
    });
    return Response.json({
      up: res.ok,
      status: res.status,
      ms: Date.now() - started,
      checkedAt: new Date().toISOString(),
    });
  } catch {
    return Response.json({
      up: false,
      status: 0,
      ms: Date.now() - started,
      checkedAt: new Date().toISOString(),
    });
  }
}
