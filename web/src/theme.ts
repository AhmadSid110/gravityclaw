import { useEffect, useState } from "react";

export type ThemeId = "midnight" | "oled" | "nordic" | "warm" | "light" | "tokyo";
export type FontId = "sans" | "modern" | "inter" | "editorial" | "mono" | "system";
export type DensityId = "comfortable" | "compact" | "touch";

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
    name: "Midnight Slate",
    description: "Deep obsidian dark with calm steel blue accents. Minimalist and focused.",
    bg: "#090a0f",
    surface: "#10131b",
    accent: "#3b82f6",
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
    name: "Nordic Zinc",
    description: "Scandinavian neutral graphite with muted sky blue accents. Clean and subtle.",
    bg: "#101114",
    surface: "#17181d",
    accent: "#38bdf8",
    isDark: true,
  },
  {
    id: "warm",
    name: "Warm Espresso",
    description: "Refined dark coffee tone with muted amber accents. Gentle on the eyes.",
    bg: "#11100e",
    surface: "#181613",
    accent: "#d97706",
    isDark: true,
  },
  {
    id: "light",
    name: "Clean Porcelain",
    description: "Pure document white with crisp neutral slate & sapphire accents. High clarity.",
    bg: "#f8fafc",
    surface: "#ffffff",
    accent: "#2563eb",
    isDark: false,
  },
  {
    id: "tokyo",
    name: "Tokyo Twilight",
    description: "Subtle indigo night with soft muted iris accents. Calm and understated.",
    bg: "#0c0d15",
    surface: "#121420",
    accent: "#818cf8",
    isDark: true,
  },
];

export const FONTS: FontConfig[] = [
  {
    id: "sans",
    name: "Pure Sans-Serif",
    description: "Neutral, ultra-clean sans-serif (Helvetica / SF Pro / Roboto)",
    sample: "Maximum clarity, neutral tone, and distraction-free readability.",
  },
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


export type FontSizeId = "small" | "medium" | "large" | "xlarge";

export interface FontSizeConfig {
  id: FontSizeId;
  name: string;
  label: string;
  description: string;
  scale: string;
}

export const FONT_SIZES: FontSizeConfig[] = [
  {
    id: "small",
    name: "Small",
    label: "11px",
    description: "Ultra-compact text size for maximum information density",
    scale: "0.80",
  },
  {
    id: "medium",
    name: "Medium",
    label: "14px",
    description: "Standard balanced text size for everyday reading",
    scale: "1.0",
  },
  {
    id: "large",
    name: "Large",
    label: "15.5px",
    description: "Enhanced legibility with larger text and message bubbles",
    scale: "1.1",
  },
  {
    id: "xlarge",
    name: "Extra Large",
    label: "17px",
    description: "Maximum readability and comfortable accessibility sizing",
    scale: "1.2",
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
  {
    id: "touch",
    name: "Touch / Accessibility",
    description: "Generous touch targets and relaxed spacing for mobile & tablets",
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

export function getInitialFontSize(): FontSizeId {
  try {
    const saved = localStorage.getItem("gravityclaw-font-size") as FontSizeId;
    if (FONT_SIZES.some((s) => s.id === saved)) return saved;
  } catch {}
  return "medium";
}

export function applyFontSize(fontSize: FontSizeId) {
  try {
    localStorage.setItem("gravityclaw-font-size", fontSize);
  } catch {}
  document.documentElement.dataset.fontSize = fontSize;
  document.documentElement.setAttribute("data-font-size", fontSize);
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
  const [fontSize, setFontSizeState] = useState<FontSizeId>(getInitialFontSize);
  const [density, setDensityState] = useState<DensityId>(getInitialDensity);

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  useEffect(() => {
    applyFont(font);
  }, [font]);

  useEffect(() => {
    applyFontSize(fontSize);
  }, [fontSize]);

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

  const setFontSize = (s: FontSizeId) => {
    setFontSizeState(s);
    applyFontSize(s);
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
    fontSize,
    setFontSize,
    density,
    setDensity,
    currentThemeConfig: THEMES.find((t) => t.id === theme) || THEMES[0],
    currentFontConfig: FONTS.find((f) => f.id === font) || FONTS[0],
    currentFontSizeConfig: FONT_SIZES.find((s) => s.id === fontSize) || FONT_SIZES[1],
    currentDensityConfig: DENSITIES.find((d) => d.id === density) || DENSITIES[0],
  };
}
