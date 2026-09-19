import { PrismaAdapter } from "@auth/prisma-adapter";
import type { NextAuthOptions } from "next-auth";
import GoogleProvider from "next-auth/providers/google";
import { prisma } from "@/lib/prisma";

// Database session strategy (not JWT): sessions and linked Google accounts
// both persist in Postgres, which is what makes "connect once" survive
// across stateless Vercel function invocations -- see CLAUDE.md.
//
// Scope is deliberately just openid/email/profile (the provider default)
// -- no drive.file yet, no `access_type: "offline"`, so no refresh_token
// is issued or stored yet. That, plus encrypting it at rest, lands with
// the Drive-upload feature.
export const authOptions: NextAuthOptions = {
  adapter: PrismaAdapter(prisma),
  session: { strategy: "database" },
  providers: [
    GoogleProvider({
      clientId: process.env.GOOGLE_CLIENT_ID!,
      clientSecret: process.env.GOOGLE_CLIENT_SECRET!,
    }),
  ],
  callbacks: {
    session({ session, user }) {
      if (session.user) {
        session.user.id = user.id;
      }
      return session;
    },
  },
};
