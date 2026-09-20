import { getServerSession } from "next-auth";
import { NextResponse } from "next/server";
import { authOptions } from "@/lib/auth";
import { prisma } from "@/lib/prisma";
import { saveTranslationToDrive } from "@/lib/drive";

// Called by the frontend right after a successful /api/translate, only
// when the user's Drive-autosave toggle is on -- purely additive, the
// existing download-button flow never depends on this route succeeding.
export async function POST(request: Request): Promise<NextResponse> {
  const session = await getServerSession(authOptions);
  if (!session?.user?.id) {
    return NextResponse.json({ error: "Not signed in." }, { status: 401 });
  }

  const user = await prisma.user.findUnique({ where: { id: session.user.id } });
  if (!user?.driveAutosaveEnabled) {
    return NextResponse.json({ error: "Drive autosave is not enabled." }, { status: 400 });
  }

  const body = (await request.json()) as { blob_url?: string; filename?: string };
  if (!body.blob_url || !body.filename) {
    return NextResponse.json(
      { error: "blob_url and filename are required." },
      { status: 400 }
    );
  }

  try {
    await saveTranslationToDrive(session.user.id, body.blob_url, body.filename);
    return NextResponse.json({ ok: true });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Drive save failed." },
      { status: 500 }
    );
  }
}
