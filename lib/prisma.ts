import { PrismaClient } from "@/app/generated/prisma/client";
import { PrismaPg } from "@prisma/adapter-pg";

// Prisma 7 requires a driver adapter to instantiate the client -- see
// CLAUDE.md's Prisma 7 section. Runtime queries use the pooled
// DATABASE_URL (correct for many concurrent serverless invocations);
// migrations use DIRECT_URL via prisma7.config.ts instead.
const globalForPrisma = globalThis as unknown as {
  prisma: PrismaClient | undefined;
};

const adapter = new PrismaPg({ connectionString: process.env.DATABASE_URL });

export const prisma =
  globalForPrisma.prisma ?? new PrismaClient({ adapter });

// Next.js dev hot-reload re-evaluates this module on every edit; caching
// the client on `global` avoids exhausting Neon's connection pool.
if (process.env.NODE_ENV !== "production") {
  globalForPrisma.prisma = prisma;
}
