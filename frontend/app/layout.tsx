import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Dashboard",
  description: "NIFTY — ATR Intraday",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="bg-[#f0f2f5] text-gray-900 antialiased">{children}</body>
    </html>
  );
}
