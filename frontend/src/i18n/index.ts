import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';
import en from './locales/en.json';
import he from './locales/he.json';
import type { Language } from '../api/types';

export const LANG_KEY = 'netmon.lang';
export const LANGUAGES: Language[] = ['en', 'he'];

export function storedLanguage(): Language | null {
  try {
    const v = localStorage.getItem(LANG_KEY);
    return v === 'he' || v === 'en' ? v : null;
  } catch {
    return null;
  }
}

export function applyDocumentLanguage(lang: Language): void {
  const html = document.documentElement;
  html.lang = lang;
  html.dir = lang === 'he' ? 'rtl' : 'ltr';
}

/** Switches the UI language, persists it, and updates <html dir/lang>. */
export function setLanguage(lang: Language, persist = true): void {
  if (persist) {
    try {
      localStorage.setItem(LANG_KEY, lang);
    } catch {
      /* ignore */
    }
  }
  applyDocumentLanguage(lang);
  if (i18n.language !== lang) void i18n.changeLanguage(lang);
}

const initial = storedLanguage() ?? 'en';

void i18n.use(initReactI18next).init({
  resources: { en: { translation: en }, he: { translation: he } },
  lng: initial,
  fallbackLng: 'en',
  interpolation: { escapeValue: false },
  returnNull: false,
});
applyDocumentLanguage(initial);

export default i18n;
