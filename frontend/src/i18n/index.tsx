import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { zh } from "./zh";
import { en } from "./en";

export type Lang = "zh" | "en";
export type MessageKey = keyof typeof zh;
export type Messages = Record<MessageKey, string>;

const RESOURCES: Record<Lang, Messages> = { zh, en };

const STORAGE_KEY = "acs-lang";

let currentLang: Lang = "zh";

function detectLang(): Lang {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved === "zh" || saved === "en") return saved;
  } catch {
    /* ignore */
  }
  return "zh";
}

export function translate(key: MessageKey, vars?: Record<string, string | number>): string {
  const table = RESOURCES[currentLang] ?? zh;
  let s = table[key] ?? zh[key] ?? key;
  if (vars) {
    for (const [k, v] of Object.entries(vars)) {
      s = s.split(`{${k}}`).join(String(v));
    }
  }
  return s;
}

interface I18nContextValue {
  lang: Lang;
  setLang: (lang: Lang) => void;
  t: typeof translate;
}

const I18nContext = createContext<I18nContextValue | null>(null);

export function I18nProvider({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<Lang>(() => {
    const l = detectLang();
    currentLang = l;
    return l;
  });

  const setLang = useCallback((l: Lang) => {
    currentLang = l;
    try {
      localStorage.setItem(STORAGE_KEY, l);
    } catch {
      /* ignore */
    }
    setLangState(l);
  }, []);

  useEffect(() => {
    currentLang = lang;
    document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
  }, [lang]);

  const value = useMemo<I18nContextValue>(
    () => ({ lang, setLang, t: translate }),
    [lang, setLang],
  );

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nContextValue {
  const ctx = useContext(I18nContext);
  if (!ctx) {
    return { lang: currentLang, setLang: () => {}, t: translate };
  }
  return ctx;
}

export { translate as t };
