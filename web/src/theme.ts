import { useEffect, useState } from "react";

export type ThemeId = "midnight" | "oled" | "nordic" | "warm" | "light" | "tokyo";
export type FontId = "modern" | "inter" | "editorial" | "mono" | "system";
export type DensityId = "comfortable" | "compact";

export interface ThemeConfig {
  id: ThemeId;
  name: string;
  description: string;
  bg: string;
  surface: string;
  accent: string;
  isDark: boolean;
}

export interface FontConfig {
  id: FontId;
  name: string;
  description: string;
  sample: string;
}

export interface DensityConfig {
  id: DensityId;
  name: string;
  description: string;
}

export const THEMES: ThemeConfig[] = [
  {
    id: "midnight",
    name: "Midnight Stealth",
    description: "Deep carbon obsidian with electric indigo accents. Calm and focused.",
    bg: "#0a0d14",
    surface: "#111622",
    accent: "#4f7df9",
    isDark: true,
  },
  {
    id: "oled",
    name: "OLED Pure Black",
    description: "True black background with cyber mint accents. Maximum contrast.",
    bg: "#000000",
    surface: "#0b0b0d",
    accent: "#00f0a8",
    isDark: true,
  },
  {
    id: "nordic",
    name: "Nordic Slate",
    description: "Cool graphite and arctic blue accents. Balanced and modern.",
    bg: "#14171d",
    surface: "#1c2028",
    accent: "#61afef",
    isDark: true,
  },
  {
    id: "warm",
    name: "Warm Sepia",
    description: "Retro-terminal dark with golden amber accents. Low eye fatigue.",
    bg: "#13110e",
    surface: "#1b1814",
    accent: "#e5a93c",
    isDark: true,
  },
  {
    id: "light",
    name: "Clean Porcelain",
    description: "Crisp white and sapphire blue accents. Document-like clarity.",
    bg: "#f8fafc",
    surface: "#ffffff",
    accent: "#2563eb",
    isDark: false,
  },
  {
    id: "tokyo",
    name: "Tokyo Dusk",
    description: "Deep navy twilight with magenta accents. Restrained cyberpunk.",
    bg: "#0d0c18",
    surface: "#151326",
    accent: "#e056fd",
    isDark: true,
  },
];

export const FONTS: FontConfig[] = [
  {
    id: "modern",
    name: "Modern Sans",
    description: "Plus Jakarta Sans — Geometric, crisp, universally legible",
    sample: "Building autonomous systems with precision.",
  },
  {
    id: "inter",
    name: "Inter Geometric",
    description: "Inter — High-density dashboard precision and clarity",
    sample: "Optimized for structured data and code inspection.",
  },
  {
    id: "editorial",
    name: "Editorial Serif",
    description: "Lora Headings + Sans Body — Elegant literary minimalism",
    sample: "Clarity in long-form thinking and analysis.",
  },
  {
    id: "mono",
    name: "Developer Mono",
    description: "JetBrains Mono UI + Readable Body — Technical developer aesthetic",
    sample: "const agent = await GravityClaw.init({ mode: 'pro' });",
  },
  {
    id: "system",
    name: "System Native",
    description: "OS Native Stack — Zero network payload, maximum responsiveness",
    sample: "Fast, native, seamless operating system integration.",
  },
];

export const DENSITIES: DensityConfig[] = [
  {
    id: "comfortable",
    name: "Comfortable",
    description: "Balanced spacing and touch targets across all displays",
  },
  {
    id: "compact",
    name: "Compact",
    description: "High information density for maximum screen utilization",
  },
];

export function getInitialTheme(): ThemeId {
  try {
    const saved = localStorage.getItem("gravityclaw-theme") as ThemeId;
    if (THEMES.some((t) => t.id === saved)) return saved;
  } catch {}
  return "midnight";
}

export function getInitialFont(): FontId {
  try {
    const saved = localStorage.getItem("gravityclaw-font") as FontId;
    if (FONTS.some((f) => f.id === saved)) return saved;
  } catch {}
  return "modern";
}

export function getInitialDensity(): DensityId {
  try {
    const saved = localStorage.getItem("gravityclaw-density") as DensityId;
    if (DENSITIES.some((d) => d.id === saved)) return saved;
  } catch {}
  return "comfortable";
}

export function applyTheme(theme: ThemeId) {
  try {
    localStorage.setItem("gravityclaw-theme", theme);
  } catch {}
  document.documentElement.dataset.theme = theme;
  const cfg = THEMES.find((t) => t.id === theme);
  if (cfg) {
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", cfg.bg);
  }
}

export function applyFont(font: FontId) {
  try {
    localStorage.setItem("gravityclaw-font", font);
  } catch {}
  document.documentElement.dataset.font = font;
}

export function applyDensity(density: DensityId) {
  try {
    localStorage.setItem("gravityclaw-density", density);
  } catch {}
  document.documentElement.dataset.density = density;
}

export function useAppearance() {
  const [theme, setThemeState] = useState<ThemeId>(getInitialTheme);
  const [font, setFontState] = useState<FontId>(getInitialFont);
  const [density, setDensityState] = useState<DensityId>(getInitialDensity);

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  useEffect(() => {
    applyFont(font);
  }, [font]);

  useEffect(() => {
    applyDensity(density);
  }, [density]);

  const setTheme = (t: ThemeId) => {
    setThemeState(t);
    applyTheme(t);
  };

  const setFont = (f: FontId) => {
    setFontState(f);
    applyFont(f);
  };

  const setDensity = (d: DensityId) => {
    setDensityState(d);
    applyDensity(d);
  };

  return {
    theme,
    setTheme,
    font,
    setFont,
    density,
    setDensity,
    currentThemeConfig: THEMES.find((t) => t.id === theme) || THEMES[0],
    currentFontConfig: FONTS.find((f) => f.id === font) || FONTS[0],
    currentDensityConfig: DENSITIES.find((d) => d.id === density) || DENSITIES[0],
  };
}
