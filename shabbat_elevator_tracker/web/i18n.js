// Strings files: i18n/<lang>.json. Every user-visible text comes from them.

export const LANGS = ["en", "he", "fr"];
const cache = {};

export async function loadStrings(lang) {
  if (!LANGS.includes(lang)) lang = "en";
  if (!cache[lang]) {
    const r = await fetch(new URL(`./i18n/${lang}.json`, import.meta.url));
    cache[lang] = await r.json();
  }
  if (lang !== "en" && !cache.en) {
    const r = await fetch(new URL("./i18n/en.json", import.meta.url));
    cache.en = await r.json();
  }
  return cache[lang];
}

export function autoLang() {
  for (const l of navigator.languages || [navigator.language || "en"]) {
    const base = String(l).toLowerCase().split("-")[0];
    if (base === "iw") return "he";
    if (LANGS.includes(base)) return base;
  }
  return "en";
}

export class Translator {
  constructor(lang, strings) {
    this.lang = lang;
    this.s = strings;
    this.en = cache.en || strings;
  }

  has(key) {
    return key in this.s;
  }

  t(key, vars) {
    let v = this.s[key];
    if (v === undefined) v = this.en[key];
    if (v === undefined) return key;
    if (vars) v = v.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k]) : m));
    return v;
  }

  get dir() {
    return this.s["_meta.dir"] || "ltr";
  }
}
