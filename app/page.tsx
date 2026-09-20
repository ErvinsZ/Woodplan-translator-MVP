"use client";

import { upload } from "@vercel/blob/client";
import { useSession } from "next-auth/react";
import { useCallback, useRef, useState } from "react";

type Status = "idle" | "uploading" | "translating" | "done" | "error";

type DriveSaveStatus = "idle" | "saving" | "saved" | "error";

type Lang = "de" | "es" | "fr";

const LANGUAGES: { code: Lang; label: string; adjective: string }[] = [
  { code: "de", label: "DE", adjective: "German" },
  { code: "es", label: "ES", adjective: "Spanish" },
  { code: "fr", label: "FR", adjective: "French" },
];

function formatBytes(bytes: number) {
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export default function Home() {
  const { data: session } = useSession();
  const [file, setFile] = useState<File | null>(null);
  const [lang, setLang] = useState<Lang>("de");
  const [status, setStatus] = useState<Status>("idle");
  const [errorMessage, setErrorMessage] = useState("");
  const [downloadUrl, setDownloadUrl] = useState<string | null>(null);
  const [downloadName, setDownloadName] = useState("");
  const [dragActive, setDragActive] = useState(false);
  // null = no pending toggle click, follow the account-level setting
  // from the session (persists across sessions/devices -- "connect
  // once, auto-save forever"). Set only optimistically while a toggle
  // click is in flight or just failed, not synced from session via an
  // effect (that pattern causes an extra render on every session poll).
  const [pendingDriveEnabled, setPendingDriveEnabled] = useState<boolean | null>(null);
  const [driveSaveStatus, setDriveSaveStatus] = useState<DriveSaveStatus>("idle");
  const inputRef = useRef<HTMLInputElement>(null);

  const driveEnabled = pendingDriveEnabled ?? session?.user?.driveAutosaveEnabled ?? false;

  const toggleDrive = useCallback(async () => {
    const next = !driveEnabled;
    setPendingDriveEnabled(next);
    try {
      await fetch("/api/drive/toggle", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: next }),
      });
    } catch {
      setPendingDriveEnabled(!next);
    }
  }, [driveEnabled]);

  const reset = useCallback(() => {
    setFile(null);
    setStatus("idle");
    setErrorMessage("");
    setDownloadUrl(null);
    setDownloadName("");
    setDriveSaveStatus("idle");
  }, []);

  const pickFile = useCallback((picked: File | null) => {
    if (!picked) return;
    if (picked.type !== "application/pdf") {
      setStatus("error");
      setErrorMessage("Only PDF files are supported right now.");
      return;
    }
    setFile(picked);
    setStatus("idle");
    setErrorMessage("");
    setDownloadUrl(null);
  }, []);

  const translate = useCallback(async () => {
    if (!file) return;
    setErrorMessage("");
    try {
      // Uploads straight to Blob storage from the browser, bypassing
      // every Vercel Function's 4.5MB request-body cap.
      setStatus("uploading");
      const uploaded = await upload(file.name, file, {
        access: "public",
        handleUploadUrl: "/api/upload-token",
      });

      setStatus("translating");
      const res = await fetch(`/api/translate?lang=${lang}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ blob_url: uploaded.url, filename: file.name }),
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data?.error ?? "Translation failed. Please try again.");
      }
      setDownloadUrl(data.download_url);
      setDownloadName(data.filename);
      setStatus("done");

      // Purely additive -- the download button above already has the
      // file regardless of whether this succeeds.
      if (driveEnabled) {
        setDriveSaveStatus("saving");
        try {
          const driveRes = await fetch("/api/drive/save", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ blob_url: data.download_url, filename: data.filename }),
          });
          setDriveSaveStatus(driveRes.ok ? "saved" : "error");
        } catch {
          setDriveSaveStatus("error");
        }
      }
    } catch (err) {
      setStatus("error");
      setErrorMessage(
        err instanceof Error ? err.message : "Translation failed. Please try again."
      );
    }
  }, [file, lang, driveEnabled]);

  const selectedLanguage = LANGUAGES.find((l) => l.code === lang)!;

  return (
    <div className="flex flex-1 items-center justify-center px-6 py-16">
      <div className="w-full max-w-xl border border-line-strong bg-paper shadow-[6px_6px_0_var(--color-line)]">
        {/* title block, styled after the plan's own corner stamp */}
        <div className="flex items-center justify-between border-b border-line-strong px-5 py-3 font-mono text-[11px] uppercase tracking-[0.14em] text-muted">
          <span>Woodplan Translator</span>
          <span className="text-accent">EN &rarr; {selectedLanguage.label}</span>
        </div>

        <div className="px-6 py-8 sm:px-9 sm:py-10">
          <h1 className="font-display text-[28px] font-medium leading-tight text-foreground sm:text-[32px]">
            Translate a build plan
          </h1>
          <p className="mt-3 max-w-md text-[15px] leading-6 text-muted">
            Upload a PDF. Dimensions, part codes, and cutout lists stay
            exactly where they are on the page &mdash; only the words move.
          </p>

          <div className="mt-6 flex gap-2" role="radiogroup" aria-label="Target language">
            {LANGUAGES.map(({ code, label, adjective }) => (
              <button
                key={code}
                type="button"
                role="radio"
                aria-checked={lang === code}
                disabled={status === "uploading" || status === "translating"}
                onClick={() => setLang(code)}
                title={adjective}
                className={`border px-3 py-1.5 font-mono text-xs uppercase tracking-[0.1em] transition-colors disabled:opacity-60 ${
                  lang === code
                    ? "border-blueprint bg-blueprint text-paper"
                    : "border-line-strong text-muted hover:border-blueprint hover:text-foreground"
                }`}
              >
                {label}
              </button>
            ))}
          </div>

          {session && (
            <label className="mt-4 flex w-fit cursor-pointer items-center gap-2 font-mono text-xs uppercase tracking-[0.1em] text-muted hover:text-foreground">
              <input
                type="checkbox"
                checked={driveEnabled}
                onChange={toggleDrive}
                className="h-3.5 w-3.5 accent-blueprint"
              />
              Also save to Google Drive
            </label>
          )}

          <div className="mt-6">
            {!file && (
              <label
                onDragOver={(e) => {
                  e.preventDefault();
                  setDragActive(true);
                }}
                onDragLeave={() => setDragActive(false)}
                onDrop={(e) => {
                  e.preventDefault();
                  setDragActive(false);
                  pickFile(e.dataTransfer.files?.[0] ?? null);
                }}
                className={`flex cursor-pointer flex-col items-center gap-2 border-2 border-dashed px-6 py-12 text-center transition-colors ${
                  dragActive
                    ? "border-accent bg-accent/5"
                    : "border-line-strong hover:border-blueprint"
                }`}
              >
                <span className="font-mono text-xs uppercase tracking-[0.1em] text-muted">
                  Drop PDF here, or
                </span>
                <span className="font-mono text-sm font-medium text-blueprint underline underline-offset-4">
                  browse files
                </span>
                <input
                  ref={inputRef}
                  type="file"
                  accept="application/pdf"
                  className="sr-only"
                  onChange={(e) => pickFile(e.target.files?.[0] ?? null)}
                />
              </label>
            )}

            {file && (
              <div className="border border-line-strong px-5 py-4">
                <div className="flex items-center justify-between gap-4">
                  <div className="min-w-0">
                    <p className="truncate font-mono text-sm text-foreground">
                      {file.name}
                    </p>
                    <p className="mt-0.5 font-mono text-xs text-muted">
                      {formatBytes(file.size)}
                    </p>
                  </div>
                  {status !== "uploading" && status !== "translating" && (
                    <button
                      type="button"
                      onClick={reset}
                      className="shrink-0 font-mono text-xs uppercase tracking-[0.1em] text-muted underline underline-offset-4 hover:text-foreground"
                    >
                      Remove
                    </button>
                  )}
                </div>

                {status !== "done" && (
                  <button
                    type="button"
                    onClick={translate}
                    disabled={status === "uploading" || status === "translating"}
                    className="mt-4 w-full bg-accent px-4 py-3 font-mono text-sm font-medium uppercase tracking-[0.08em] text-accent-foreground transition-opacity disabled:opacity-60"
                  >
                    {status === "uploading"
                      ? "Uploading…"
                      : status === "translating"
                        ? "Translating…"
                        : `Translate to ${selectedLanguage.adjective}`}
                  </button>
                )}

                {(status === "uploading" || status === "translating") && (
                  <div className="mt-3 h-[2px] w-full overflow-hidden bg-line">
                    <div className="h-full w-1/3 animate-[scan_1.1s_ease-in-out_infinite] bg-blueprint" />
                  </div>
                )}

                {status === "done" && downloadUrl && (
                  <div className="mt-4 flex flex-col gap-2 sm:flex-row">
                    <a
                      href={downloadUrl}
                      download={downloadName}
                      className="flex-1 bg-blueprint px-4 py-3 text-center font-mono text-sm font-medium uppercase tracking-[0.08em] text-paper"
                    >
                      Download translated PDF
                    </a>
                    <button
                      type="button"
                      onClick={reset}
                      className="px-4 py-3 font-mono text-xs uppercase tracking-[0.1em] text-muted underline underline-offset-4 hover:text-foreground"
                    >
                      Translate another
                    </button>
                  </div>
                )}

                {driveSaveStatus !== "idle" && (
                  <p className="mt-3 font-mono text-xs uppercase tracking-[0.1em] text-muted">
                    {driveSaveStatus === "saving" && "Saving to Google Drive…"}
                    {driveSaveStatus === "saved" && "Saved to Google Drive."}
                    {driveSaveStatus === "error" && "Drive save failed — file is still available above."}
                  </p>
                )}
              </div>
            )}

            {status === "error" && (
              <p className="mt-4 border-l-2 border-accent pl-3 text-sm leading-6 text-foreground">
                {errorMessage}
              </p>
            )}
          </div>
        </div>

        <div className="border-t border-line-strong px-5 py-2.5 font-mono text-[10px] uppercase tracking-[0.14em] text-muted">
          Glossary + AI fallback &middot; unresolved text flagged, not guessed
        </div>
      </div>
    </div>
  );
}
