import type { Metadata, Viewport } from "next";
import "./globals.css";

// Favicon is auto-wired by Next.js App Router convention:
//   frontend/app/icon.jpeg       → <link rel="icon">
//   frontend/app/apple-icon.jpeg → <link rel="apple-touch-icon">
export const metadata: Metadata = {
  title: {
    default: "The Gaint Company — Trading Bot",
    template: "%s · The Gaint Company",
  },
  description: "Automated price-action trading on Delta India and Angel One",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",   // lets safe-area insets work on notched phones
  themeColor: "#f9f9f7",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="antialiased">{children}</body>
    </html>
  );
}
