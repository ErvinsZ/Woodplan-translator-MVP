import type { Metadata } from "next";
import Link from "next/link";
import { Space_Grotesk, IBM_Plex_Mono } from "next/font/google";
import { AuthStatus } from "./components/AuthStatus";
import { Providers } from "./providers";
import "./globals.css";

const spaceGrotesk = Space_Grotesk({
  variable: "--font-display",
  subsets: ["latin"],
});

const plexMono = IBM_Plex_Mono({
  variable: "--font-mono",
  weight: ["400", "500", "600"],
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Woodplan Translator",
  description:
    "Translate Etsy woodworking build plans to German while keeping every dimension, part code, and cutout list exactly where it belongs.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      className={`${spaceGrotesk.variable} ${plexMono.variable} h-full antialiased`}
    >
      <body className="min-h-full flex flex-col">
        <Providers>
          <div className="flex justify-end px-6 py-4">
            <AuthStatus />
          </div>
          {children}
          <footer className="px-6 py-4 text-center font-mono text-[11px] uppercase tracking-[0.1em] text-muted">
            <Link href="/privacy" className="underline underline-offset-4 hover:text-foreground">
              Privacy Policy
            </Link>
          </footer>
        </Providers>
      </body>
    </html>
  );
}
