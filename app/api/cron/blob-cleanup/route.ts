import { del, list } from "@vercel/blob";
import { NextResponse } from "next/server";

// Blob is only a short-lived handoff: the browser uploads the source PDF
// there, api/translate.py writes the translated PDF there, and the page's
// Download link plus the Drive autosave both fetch it back from its URL.
// Nothing needs a blob after that, but nothing deleted them either --
// translated outputs (and source uploads orphaned by a crashed request)
// piled up toward the store's 1GB free-tier cap. Run daily by Vercel Cron
// (vercel.json); deletes every blob older than MAX_AGE_HOURS.
const MAX_AGE_HOURS = 24;
const DELETE_BATCH = 100;
// list() is eventually consistent, in both directions (seen against the
// real store): a full listing reported 37 blobs with hasMore=false, and
// after deleting the 30 expired ones, 11 more expired blobs appeared that
// it had never returned; and blobs deleted moments earlier (their URLs
// already 404) keep appearing in listings for a while. So repeat
// list+delete until a pass finds nothing new (bounded), and treat a
// re-listed, already-deleted blob as harmless -- deleting it again is a
// no-op. `freedBytes` can therefore over-count right after a previous run.
const MAX_PASSES = 10;

export async function GET(request: Request): Promise<NextResponse> {
  // Vercel Cron sends `Authorization: Bearer $CRON_SECRET` when the env var
  // is set. Refuse to run without it rather than expose a delete endpoint.
  const secret = process.env.CRON_SECRET;
  if (!secret) {
    return NextResponse.json({ error: "CRON_SECRET is not configured." }, { status: 500 });
  }
  if (request.headers.get("authorization") !== `Bearer ${secret}`) {
    return NextResponse.json({ error: "Unauthorized." }, { status: 401 });
  }

  const dryRun = new URL(request.url).searchParams.get("dryRun") === "1";
  const cutoff = Date.now() - MAX_AGE_HOURS * 60 * 60 * 1000;

  const seen = new Map<string, number>(); // url -> size, every expired blob found
  let passes = 0;
  while (passes < MAX_PASSES) {
    passes++;
    const found: string[] = [];
    let cursor: string | undefined;
    do {
      const page = await list({ cursor, limit: 1000 });
      for (const blob of page.blobs) {
        if (blob.uploadedAt.getTime() < cutoff && !seen.has(blob.url)) {
          seen.set(blob.url, blob.size);
          found.push(blob.url);
        }
      }
      cursor = page.hasMore ? page.cursor : undefined;
    } while (cursor);

    if (found.length === 0 || dryRun) break;
    for (let i = 0; i < found.length; i += DELETE_BATCH) {
      await del(found.slice(i, i + DELETE_BATCH));
    }
  }

  const freedBytes = [...seen.values()].reduce((sum, size) => sum + size, 0);
  return NextResponse.json({
    dryRun,
    maxAgeHours: MAX_AGE_HOURS,
    passes,
    expired: seen.size,
    deleted: dryRun ? 0 : seen.size,
    freedBytes,
  });
}
