import type { Metadata } from "next";
import "./globals.css";

// Favicon is auto-wired by Next.js App Router convention:
//   frontend/app/icon.svg       → <link rel="icon" type="image/svg+xml">
//   frontend/app/apple-icon.svg → <link rel="apple-touch-icon">
// Both files are copies of the brand logo from frontend/public/tgc-logo-svg.svg.
export const metadata: Metadata = {
  title: {
    default:  "The Gaint Company — Trading Bot",
    template: "%s · The Gaint Company",
  },
  description: "Crypto price-action S/R retest — BTC/ETH perp dashboard",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="bg-[#f0f2f5] text-gray-900 antialiased">{children}</body>
    </html>
  );
}
