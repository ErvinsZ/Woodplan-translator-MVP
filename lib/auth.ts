import { PrismaAdapter } from "@auth/prisma-adapter";
import type { Adapter, AdapterAccount } from "next-auth/adapters";
import type { NextAuthOptions } from "next-auth";
import GoogleProvider from "next-auth/providers/google";
import { prisma } from "@/lib/prisma";
import { encrypt } from "@/lib/crypto";

// Database session strategy (not JWT): sessions and linked Google accounts
// both persist in Postgres, which is what makes "connect once" survive
// across stateless Vercel function invocations -- see CLAUDE.md.
//
// Scope includes drive.file + offline access, requested upfront at every
// sign-in (not as a separate incremental-consent flow triggered by the
// Drive-autosave toggle) -- deliberately simpler than incremental auth:
// reuses NextAuth's existing account-linking/token storage as-is, no
// second OAuth callback route, no CSRF state to manage. Trade-off: every
// user sees the Drive permission request at sign-in, and `prompt:
// "consent"` means they see Google's consent screen on EVERY sign-in
// (not just the first), not only those who use the toggle -- accepted
// for MVP simplicity. `prompt: "consent"` guarantees Google actually
// returns a refresh_token (Google can omit it otherwise on repeat
// authorizations), which the Drive-autosave feature depends on.
function encryptingAdapter(): Adapter {
  const base = PrismaAdapter(prisma);
  return {
    ...base,
    linkAccount(account: AdapterAccount) {
      // `as any`: next-auth/adapters' AdapterAccount and
      // @auth/prisma-adapter's own (separately-versioned) @auth/core
      // AdapterAccount disagree only on token_type's type-level
      // branding (`string` vs `Lowercase<string>`) -- a cross-package
      // type-declaration mismatch, not a real runtime shape difference.
      return base.linkAccount!({
        ...account,
        refresh_token: account.refresh_token
          ? encrypt(account.refresh_token)
          : account.refresh_token,
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
      } as any);
    },
  };
}

export const authOptions: NextAuthOptions = {
  adapter: encryptingAdapter(),
  session: { strategy: "database" },
  providers: [
    GoogleProvider({
      clientId: process.env.GOOGLE_CLIENT_ID!,
      clientSecret: process.env.GOOGLE_CLIENT_SECRET!,
      authorization: {
        params: {
          scope:
            "openid email profile https://www.googleapis.com/auth/drive.file",
          access_type: "offline",
          prompt: "consent",
        },
      },
    }),
  ],
  callbacks: {
    session({ session, user }) {
      if (session.user) {
        session.user.id = user.id;
        session.user.driveAutosaveEnabled = Boolean(
          (user as { driveAutosaveEnabled?: boolean }).driveAutosaveEnabled
        );
      }
      return session;
    },
  },
  events: {
    // NextAuth v4's callback-handler only calls the adapter's
    // linkAccount for a BRAND NEW account -- for an account that's
    // already linked (e.g. every user who signed in before this Drive
    // feature existed), it skips linkAccount entirely and just creates
    // a session, silently discarding whatever fresh tokens this
    // sign-in's re-consent just produced (confirmed by reading
    // node_modules/next-auth/core/lib/callback-handler.js directly).
    // Without this, re-signing-in would never actually pick up a Drive
    // refresh_token for an already-linked account. `account` here is
    // the raw object straight from the OAuth exchange, independent of
    // whatever the adapter did with it, so this always has the fresh
    // token when Google issued one.
    async signIn({ user, account }) {
      if (account?.provider !== "google" || !account.refresh_token || !user.id) {
        return;
      }
      await prisma.account.update({
        where: {
          provider_providerAccountId: {
            provider: "google",
            providerAccountId: account.providerAccountId,
          },
        },
        data: { refresh_token: encrypt(account.refresh_token) },
      });
    },
  },
};
