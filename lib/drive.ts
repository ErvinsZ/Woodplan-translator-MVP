import { prisma } from "@/lib/prisma";
import { decrypt } from "@/lib/crypto";

// Hand-rolled Drive v3 REST calls, no googleapis client -- consistent
// with api/blob.py's and api/gemini_translate.py's existing pattern of
// avoiding heavy SDKs, and this is the Node side so no compiled-Python-
// extension risk either way, just keeping the pattern consistent.
const DRIVE_FOLDER_NAME = "Woodplan Translator";
const TOKEN_URL = "https://oauth2.googleapis.com/token";
const DRIVE_API = "https://www.googleapis.com/drive/v3";
const DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3/files";

async function getRefreshToken(userId: string): Promise<string | null> {
  const account = await prisma.account.findFirst({
    where: { userId, provider: "google" },
  });
  if (!account?.refresh_token) return null;
  return decrypt(account.refresh_token);
}

async function refreshAccessToken(refreshToken: string): Promise<string> {
  const res = await fetch(TOKEN_URL, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      client_id: process.env.GOOGLE_CLIENT_ID!,
      client_secret: process.env.GOOGLE_CLIENT_SECRET!,
      refresh_token: refreshToken,
      grant_type: "refresh_token",
    }),
  });
  if (!res.ok) {
    throw new Error(`Google token refresh failed: ${res.status} ${await res.text()}`);
  }
  const data = (await res.json()) as { access_token: string };
  return data.access_token;
}

// existingFolderId survives a folder RENAME (Drive IDs are immutable),
// only a delete/trash invalidates it -- in that case this recreates the
// folder and the caller persists the new id.
async function findOrCreateFolder(
  accessToken: string,
  existingFolderId: string | null
): Promise<string> {
  if (existingFolderId) {
    const check = await fetch(
      `${DRIVE_API}/files/${existingFolderId}?fields=id,trashed`,
      { headers: { Authorization: `Bearer ${accessToken}` } }
    );
    if (check.ok) {
      const data = (await check.json()) as { trashed?: boolean };
      if (!data.trashed) return existingFolderId;
    }
  }

  const create = await fetch(`${DRIVE_API}/files`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${accessToken}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      name: DRIVE_FOLDER_NAME,
      mimeType: "application/vnd.google-apps.folder",
    }),
  });
  if (!create.ok) {
    throw new Error(`Drive folder creation failed: ${create.status} ${await create.text()}`);
  }
  const created = (await create.json()) as { id: string };
  return created.id;
}

async function uploadFile(
  accessToken: string,
  folderId: string,
  filename: string,
  bytes: ArrayBuffer
): Promise<void> {
  const boundary = "woodplan_drive_upload_boundary";
  const metadata = JSON.stringify({ name: filename, parents: [folderId] });
  const head =
    `--${boundary}\r\n` +
    `Content-Type: application/json; charset=UTF-8\r\n\r\n${metadata}\r\n` +
    `--${boundary}\r\n` +
    `Content-Type: application/pdf\r\n\r\n`;
  const tail = `\r\n--${boundary}--`;
  const multipartBody = Buffer.concat([
    Buffer.from(head, "utf8"),
    Buffer.from(bytes),
    Buffer.from(tail, "utf8"),
  ]);

  const res = await fetch(`${DRIVE_UPLOAD_API}?uploadType=multipart`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${accessToken}`,
      "Content-Type": `multipart/related; boundary=${boundary}`,
    },
    body: multipartBody,
  });
  if (!res.ok) {
    throw new Error(`Drive upload failed: ${res.status} ${await res.text()}`);
  }
}

export async function saveTranslationToDrive(
  userId: string,
  blobUrl: string,
  filename: string
): Promise<void> {
  const refreshToken = await getRefreshToken(userId);
  if (!refreshToken) {
    throw new Error(
      "No Google Drive permission on file -- sign in again to grant Drive access."
    );
  }
  const accessToken = await refreshAccessToken(refreshToken);

  const user = await prisma.user.findUniqueOrThrow({ where: { id: userId } });
  const folderId = await findOrCreateFolder(accessToken, user.driveFolderId);
  if (folderId !== user.driveFolderId) {
    await prisma.user.update({
      where: { id: userId },
      data: { driveFolderId: folderId },
    });
  }

  const pdfRes = await fetch(blobUrl);
  if (!pdfRes.ok) {
    throw new Error(`Failed to fetch translated PDF from Blob: ${pdfRes.status}`);
  }
  const bytes = await pdfRes.arrayBuffer();

  await uploadFile(accessToken, folderId, filename, bytes);
}
