import type { Metadata } from "next";
import type { ReactNode } from "react";
import { Shell } from "@/components/Shell";
import { configuredApiOrigin } from "@/lib/api";
import "./globals.css";

export const metadata: Metadata = {
  title: "Adaptive Quant — operator dashboard",
  description: "Read-only views of the paper/shadow trading platform plus the kill switch.",
  referrer: "no-referrer",
  robots: { index: false, follow: false },
};

// The exported pages carry their own content-security policy: scripts and
// styles from this origin only, network requests only to the operator API.
// (Security headers such as frame-ancestors are set by the web server; see M13.)
const CSP = [
  "default-src 'self'",
  "script-src 'self' 'unsafe-inline'",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data:",
  "font-src 'self'",
  `connect-src 'self'${configuredApiOrigin() ? ` ${configuredApiOrigin()}` : ""}`,
  "object-src 'none'",
  "base-uri 'none'",
  "form-action 'none'",
].join("; ");

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <head>
        {process.env.NODE_ENV === "production" && (
          <meta httpEquiv="Content-Security-Policy" content={CSP} />
        )}
      </head>
      <body>
        <Shell>{children}</Shell>
      </body>
    </html>
  );
}
