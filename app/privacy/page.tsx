import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Privacy Policy | Woodplan Translator",
  description:
    "What Woodplan Translator collects, why, and how to control or remove it.",
};

export default function PrivacyPolicy() {
  return (
    <div className="flex flex-1 justify-center px-6 py-16">
      <div className="w-full max-w-2xl">
        <div className="border border-line-strong bg-paper shadow-[6px_6px_0_var(--color-line)]">
          <div className="flex items-center justify-between border-b border-line-strong px-5 py-3 font-mono text-[11px] uppercase tracking-[0.14em] text-muted">
            <span>Woodplan Translator</span>
            <span className="text-accent">Privacy Policy</span>
          </div>

          <div className="px-6 py-8 sm:px-9 sm:py-10">
            <h1 className="font-display text-[28px] font-medium leading-tight text-foreground sm:text-[32px]">
              Privacy Policy
            </h1>
            <p className="mt-2 font-mono text-xs uppercase tracking-[0.1em] text-muted">
              Last updated September 20, 2026
            </p>

            <div className="mt-8 space-y-8 text-[15px] leading-7 text-foreground">
              <section>
                <p>
                  Woodplan Translator is a small, independently run tool that
                  translates woodworking build plans from English into other
                  languages while keeping dimensions, part codes, and layout
                  intact. This page explains what information the app
                  collects, why, and what you can do about it. If something
                  here is unclear, contact the address at the bottom of this
                  page.
                </p>
              </section>

              <section>
                <h2 className="font-display text-xl font-medium text-foreground">
                  What we collect
                </h2>
                <p className="mt-3">
                  When you sign in with Google, we receive your name, email
                  address, and profile picture from your Google account. We
                  use this to identify you and keep you signed in.
                </p>
                <p className="mt-3">
                  When you upload a PDF to translate, we receive that file
                  and process it to produce a translated version.
                </p>
                <p className="mt-3">
                  If you turn on the Google Drive option, we also ask for
                  permission to create and manage files inside a single
                  folder that our app creates in your Drive. This uses
                  Google&apos;s narrowest Drive permission (called{" "}
                  <code className="font-mono text-sm">drive.file</code>),
                  which only lets us see and write files that our app itself
                  created. We cannot see, list, or touch anything else in
                  your Drive.
                </p>
              </section>

              <section>
                <h2 className="font-display text-xl font-medium text-foreground">
                  How we use it
                </h2>
                <ul className="mt-3 list-disc space-y-2 pl-5">
                  <li>To sign you in and remember your session.</li>
                  <li>
                    To extract the text from your uploaded PDF, translate it,
                    and rebuild a translated copy of the same document.
                    Translation mostly happens through a fixed glossary of
                    recurring woodworking phrases. For text that
                    isn&apos;t in the glossary, we send the specific sentence
                    to Google&apos;s Gemini AI for translation. Only the
                    sentence in question is sent, not the whole document or
                    the diagrams in it.
                  </li>
                  <li>
                    If you&apos;ve turned on Drive autosave, to save the
                    finished translation into the folder our app created in
                    your Drive.
                  </li>
                </ul>
                <p className="mt-3">
                  We don&apos;t use your data for advertising, and we
                  don&apos;t sell it to anyone.
                </p>
              </section>

              <section>
                <h2 className="font-display text-xl font-medium text-foreground">
                  Where things are stored
                </h2>
                <p className="mt-3">
                  Uploaded and translated PDFs are stored temporarily in
                  Vercel Blob storage, at a web address that is long and
                  effectively impossible to guess, rather than behind a
                  login. Account details, session information, and (if
                  Drive autosave is on) an encrypted credential that lets us
                  access your Drive folder are stored in a Postgres database
                  hosted by Neon. The original file you upload is deleted
                  once translation finishes. Translated files remain in
                  storage for as long as needed to let you download them;
                  we don&apos;t yet have an automatic deletion schedule for
                  them.
                </p>
              </section>

              <section>
                <h2 className="font-display text-xl font-medium text-foreground">
                  Who else sees it
                </h2>
                <p className="mt-3">
                  We rely on a small number of service providers to run the
                  app: Google, for sign-in, Drive access, and translation
                  through Gemini; Vercel, for hosting and file storage; and
                  Neon, for the database. Each only receives the specific
                  data needed to do its job. We don&apos;t share your data
                  with anyone beyond these providers.
                </p>
              </section>

              <section>
                <h2 className="font-display text-xl font-medium text-foreground">
                  Your choices
                </h2>
                <ul className="mt-3 list-disc space-y-2 pl-5">
                  <li>
                    You can turn Drive autosave off at any time from the main
                    page. Turning it off stops future translations from
                    being saved to Drive; it doesn&apos;t delete files
                    already saved there.
                  </li>
                  <li>
                    You can revoke this app&apos;s access to your Google
                    account entirely at{" "}
                    <span className="font-mono text-sm">
                      myaccount.google.com/permissions
                    </span>
                    , independent of anything on our end.
                  </li>
                  <li>
                    You can ask us to delete your account and associated
                    data by emailing us. We don&apos;t yet have a self-serve
                    delete button, so this is handled manually for now.
                  </li>
                </ul>
              </section>

              <section>
                <h2 className="font-display text-xl font-medium text-foreground">
                  Security
                </h2>
                <p className="mt-3">
                  All traffic to the app is encrypted in transit. The
                  credential that grants Drive access is encrypted at rest
                  and is never stored in plain text. No system is perfectly
                  secure, but we take reasonable, standard precautions to
                  protect what we store.
                </p>
              </section>

              <section>
                <h2 className="font-display text-xl font-medium text-foreground">
                  Children
                </h2>
                <p className="mt-3">
                  This app is not directed at children, and we don&apos;t
                  knowingly collect information from anyone under 13.
                </p>
              </section>

              <section>
                <h2 className="font-display text-xl font-medium text-foreground">
                  Changes to this policy
                </h2>
                <p className="mt-3">
                  If this policy changes, we&apos;ll update this page and
                  change the date at the top.
                </p>
              </section>

              <section>
                <h2 className="font-display text-xl font-medium text-foreground">
                  Contact
                </h2>
                <p className="mt-3">
                  Questions about this policy or your data can be sent to{" "}
                  <a
                    href="mailto:ervins.zagars@gmail.com"
                    className="text-blueprint underline underline-offset-4"
                  >
                    ervins.zagars@gmail.com
                  </a>
                  .
                </p>
              </section>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
