// Translations shared by the pages: static/i18n/<lang>.json, English as fallback.
//
// Language: the one picked on the page (kept on this device), else the one given
// by the server when Notosaurus runs inside the Anki add-on (Anki's language), else
// the browser's; English if we don't have a file for it.
// Adding a language = adding static/i18n/<code>.json.
//
// In templates: x-text="$t('app.review.title')", with {placeholders}:
// $t('app.photos.page', { n: 2 }), plurals with { count }: $t('common.count.cards', { count }).

const LANG_KEY = "notosaurus.lang";  // language picked on the page ("" = automatic)

const I18N = {
  lang: "en",        // language in use
  locale: "en",      // dates and numbers: the browser's variety of it ("en-GB", "fr-CA"), else the language
  messages: {},
  fallback: {},      // English, for keys missing from the language in use
  auto: "en",        // what "automatic" means here (Anki's or the browser's language)
  choice: "",        // language picked on the page, "" = automatic
  available: ["en"],
  names: {},         // code → name in that language ("Français")
};

function lookup(source, key) {
  return key.split(".").reduce((value, part) => (value && typeof value === "object" ? value[part] : undefined), source);
}

function t(key, params = {}) {
  let value = lookup(I18N.messages, key) ?? lookup(I18N.fallback, key);
  if (value && typeof value === "object" && "count" in params) {
    value = value[new Intl.PluralRules(I18N.lang).select(params.count)] ?? value.other;
  }
  if (typeof value !== "string") return key;
  return value.replace(/\{(\w+)\}/g, (match, name) => (name in params ? params[name] : match));
}

// For translations containing markup (x-html): the values are escaped, so a
// profile or voice name can't inject HTML.
function tHtml(key, params = {}) {
  const escape = (s) => String(s).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
  return t(key, Object.fromEntries(Object.entries(params).map(([k, v]) => [k, escape(v)])));
}

// Server errors come as { code, params } (see app/errors.py).
function errorMessage(detail) {
  if (typeof detail === "string") return detail;
  if (detail && detail.code) {
    const key = `errors.${detail.code}`;
    const message = t(key, detail.params ?? {});
    return message === key ? t("errors.unknown", { code: detail.code }) : message;
  }
  return t("errors.invalid");  // e.g. request validation errors
}

// One text per language, but dates and numbers as the browser's variety of it: a
// British browser gets "3 Oct, 15:15", an American one "Oct 3, 03:15 PM".
function formatLocale(lang) {
  const variety = (navigator.languages ?? [navigator.language]).find((code) => code.toLowerCase().split("-")[0] === lang);
  try {
    return variety ? Intl.getCanonicalLocales(variety)[0] : lang;
  } catch {
    return lang;  // not a valid locale
  }
}

function browserLanguage(available) {
  for (const candidate of navigator.languages ?? [navigator.language]) {
    const code = candidate.toLowerCase();
    for (const option of [code, code.split("-")[0]]) {
      if (available.includes(option)) return option;
    }
  }
  return null;
}

async function loadMessages(lang) {
  try {
    // Always ask the server (a 304 when unchanged): an old cached copy would
    // leave the new page's texts untranslated after an update.
    const res = await fetch(`i18n/${lang}.json`, { cache: "no-cache" });
    return res.ok ? await res.json() : {};
  } catch {
    return {};
  }
}

function storedChoice() {
  try {
    return localStorage.getItem(LANG_KEY) ?? "";
  } catch {
    return "";  // private browsing, blocked storage: automatic
  }
}

async function useLanguage(lang) {
  [I18N.messages, I18N.fallback] = await Promise.all([loadMessages(lang), lang === "en" ? {} : loadMessages("en")]);
  I18N.lang = lang;
  I18N.locale = formatLocale(lang);
  document.documentElement.lang = lang;
}

const i18nReady = (async () => {
  let info = { lang: null, available: ["en"], names: {} };
  try {
    info = await (await fetch("/api/lang")).json();
  } catch {}
  I18N.available = info.available;
  I18N.names = info.names ?? {};
  I18N.auto = info.lang ?? browserLanguage(info.available) ?? "en";
  const stored = storedChoice();
  I18N.choice = info.available.includes(stored) ? stored : "";
  await useLanguage(I18N.choice || I18N.auto);
})();

// Language picked on the page ("" = automatic): applied right away, without
// reloading (nothing typed is lost), and remembered on this device.
async function setLanguage(choice) {
  try {
    if (choice) localStorage.setItem(LANG_KEY, choice);
    else localStorage.removeItem(LANG_KEY);
  } catch {}
  I18N.choice = choice;
  await useLanguage(choice || I18N.auto);
  const store = Alpine.store("i18n");
  Object.assign(store, { choice, lang: I18N.lang });
  store.version++;  // re-renders every $t()
  document.dispatchEvent(new CustomEvent("i18n:changed", { detail: I18N.lang }));
}

document.addEventListener("alpine:init", () => {
  // Reading `version` makes every $t() re-render when the messages change.
  Alpine.store("i18n", { ready: false, version: 0, lang: "en", choice: "", auto: "en", available: [], names: {} });
  const track = () => Alpine.store("i18n").version;
  Alpine.magic("t", () => (key, params) => (track(), t(key, params)));
  Alpine.magic("th", () => (key, params) => (track(), tHtml(key, params)));
  i18nReady.then(() => {
    const { lang, choice, auto, available, names } = I18N;
    Object.assign(Alpine.store("i18n"), { ready: true, lang, choice, auto, available, names });
    Alpine.store("i18n").version++;
    document.documentElement.classList.add("i18n-ready");
  });
});
