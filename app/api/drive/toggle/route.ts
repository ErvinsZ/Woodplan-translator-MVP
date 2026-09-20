import { getServerSession } from "next-auth";
import { NextResponse } from "next/server";
import { authOptions } from "@/lib/auth";
import { prisma } from "@/lib/prisma";

export async function POST(request: Request): Promise<NextResponse> {
  const session = await getServerSession(authOptions);
  if (!session?.user?.id) {
    return NextResponse.json({ error: "Not signed in." }, { status: 401 });
  }

  const body = (await request.json()) as { enabled?: boolean };
  if (typeof body.enabled !== "boolean") {
    return NextResponse.json({ error: "enabled must be a boolean." }, { status: 400 });
  }

  await prisma.user.update({
    where: { id: session.user.id },
    data: { driveAutosaveEnabled: body.enabled },
  });

  return NextResponse.json({ ok: true, driveAutosaveEnabled: body.enabled });
}
