import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg:      "#0d0d14",
        surface: "#13131f",
        border:  "#1e1e30",
        accent:  "#00d4ff",
        green:   "#22c55e",
        red:     "#ef4444",
        muted:   "#6b7280",
      },
    },
  },
  plugins: [],
};
export default config;
