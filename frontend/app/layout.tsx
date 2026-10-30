import type { Metadata, Viewport } from "next";
import { Fraunces, Geist_Mono, Outfit } from "next/font/google";

import "./globals.css";
import { ChunkRecovery } from "@/components/chunk-recovery";
import { AuthProvider } from "@/lib/auth-context";
import { ThemeProvider } from "@/lib/theme-provider";
import { ToastProvider } from "@/components/ui/toast";
import {
  LANDING_TITLE,
  SITE_DESCRIPTION,
  SITE_NAME,
  SITE_URL,
} from "@/lib/site";

/**
 * The three faces every route needs (DESIGN.md section 3).
 *
 *  - Outfit is the working face: body, navigation, forms, tables, dialogs,
 *    reports and marketing copy. `body` carries it,
 *    so a component gets it without asking. It replaced Inter Tight on
 *    2026-09-28; it is a touch wider, so a label that fit before is not
 *    assumed to fit now.
 *  - Geist Mono carries reference codes, system identifiers, URLs and code.
 *    Nothing else: ordinary metadata stays in Outfit with tabular numerals.
 *  - Fraunces is the sole heading and Vivekium wordmark face. It replaces the
 *    previous split editorial setup, so headings never fall back to a second
 *    serif family on authenticated routes.
 *
 * Both are variable fonts (one file per subset carries every weight) and are
 * SIL Open Font License 1.1, which permits commercial use and self-hosting.
 * next/font downloads them at BUILD time and serves them from this origin, so
 * no request reaches a font CDN at runtime and the CSP's `font-src 'self'`
 * holds. It also emits a metric-matched fallback face, which is what keeps the
 * swap from shifting layout.
 */
const sans = Outfit({
  subsets: ["latin"],
  display: "swap",
  variable: "--font-sans",
});

const heading = Fraunces({
  subsets: ["latin"],
  display: "swap",
  variable: "--font-display",
});

const mono = Geist_Mono({
  subsets: ["latin"],
  display: "swap",
  variable: "--font-mono",
});

export const metadata: Metadata = {
  /**
   * The canonical origin, stated once. Every relative `alternates.canonical`
   * and every relative Open Graph URL in the tree is resolved against this, so
   * a page declares its path and never its host. The canonical domain is
   * readypick.ai (RBAC section 15); picready.com and pickready.app are not the
   * product's address and both have appeared in this tree before.
   */
  metadataBase: new URL(SITE_URL),
  title: {
    default: LANDING_TITLE,
    template: "%s | Vivekium",
  },
  description: SITE_DESCRIPTION,
  applicationName: SITE_NAME,
  // NO `icons` BLOCK, DELIBERATELY. It used to name three raster files that
  // were the previous logo, so every tab flew the old name's initials. They
  // are generated now by `app/icon.tsx` and `app/apple-icon.tsx`, which the
  // App Router discovers by filename and links automatically. Naming them
  // here as well would pin the OLD urls and win.
  openGraph: {
    type: "website",
    siteName: SITE_NAME,
    url: "/",
    // The same title and description the home page states, from `lib/site`,
    // so a page that inherits this card and the home page describe the
    // product in one sentence rather than two.
    title: LANDING_TITLE,
    description: SITE_DESCRIPTION,
    // No `images` entry here on purpose. `app/opengraph-image.tsx` generates
    // the card, and file-based metadata takes precedence over this object in
    // Next, so a path written here would either be ignored or would have to
    // name a file that does not exist in `public/`. There is no shipped og
    // asset, and inventing a path to one is how a card renders blank.
  },
  twitter: {
    card: "summary_large_image",
    title: LANDING_TITLE,
    description: SITE_DESCRIPTION,
    // `twitter:image` is deliberately absent for the same reason: X falls back
    // to og:image, which the generated card supplies.
  },
};

export const viewport: Viewport = {
  themeColor: [
    // The browser chrome colour must match the canvas token, or the phone's
    // status bar paints a different product above the page. These two were the
    // pre-navy palette and had been stale since the recolour.
    { media: "(prefers-color-scheme: light)", color: "#F6F2EB" },
    { media: "(prefers-color-scheme: dark)", color: "#000000" },
  ],
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className={`${sans.variable} ${heading.variable} ${mono.variable}`}
      suppressHydrationWarning
    >
      <body className="min-h-screen font-sans antialiased">
        {/* Recovers a tab whose chunks were invalidated by a server restart. */}
        <ChunkRecovery />
        <ThemeProvider>
          <AuthProvider>
            <ToastProvider>{children}</ToastProvider>
          </AuthProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
