"use client";

import { signIn, signOut, useSession } from "next-auth/react";

export function AuthStatus() {
  const { data: session, status } = useSession();

  if (status === "loading") {
    return (
      <span className="font-mono text-[11px] uppercase tracking-[0.1em] text-muted">
        &hellip;
      </span>
    );
  }

  if (!session) {
    return (
      <button
        type="button"
        onClick={() => signIn("google")}
        className="border border-line-strong px-3 py-1.5 font-mono text-[11px] uppercase tracking-[0.1em] text-muted transition-colors hover:border-blueprint hover:text-foreground"
      >
        Sign in with Google
      </button>
    );
  }

  return (
    <div className="flex items-center gap-3">
      <span className="font-mono text-[11px] uppercase tracking-[0.1em] text-muted">
        {session.user?.email}
      </span>
      <button
        type="button"
        onClick={() => signOut()}
        className="border border-line-strong px-3 py-1.5 font-mono text-[11px] uppercase tracking-[0.1em] text-muted underline underline-offset-4 transition-colors hover:border-blueprint hover:text-foreground"
      >
        Sign out
      </button>
    </div>
  );
}
