import type { Config } from "tailwindcss";

/** Colors live as CSS custom properties in app/globals.css so light/dark and
 *  contrast auditing happen in one place. Reference them as
 *  `text-[var(--ink-2)]` / `bg-[var(--surface)]` rather than adding aliases
 *  here — two sources of truth is how the old dark palette drifted. */
const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      screens: {
        xs: "420px",
      },
    },
  },
  plugins: [],
};
export default config;
