const PASSWORD_KEY = "cartable.admin";  // kept for the browser session only

// Names and descriptions: admin.provider.<id>.* in static/i18n/<lang>.json
const PROVIDERS = [
  { id: "gemini", models: ["gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.1-pro-preview"] },
  { id: "anthropic", models: ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"] },
  { id: "openai", models: ["qwen2.5vl", "gemma3"] },
  { id: "fake", models: [] },
];

// Services reachable through the OpenAI-compatible provider: picking one fills
// the address and suggests a model accepting images ("" = load the list).
// Others (Mistral, Ollama, LM Studio…) work through "Other" with their address.
const SERVICES = [
  { id: "openai", name: "OpenAI", url: "https://api.openai.com/v1", model: "gpt-6-luna",
    keyUrl: "https://platform.openai.com/api-keys" },
  { id: "openrouter", name: "OpenRouter", url: "https://openrouter.ai/api/v1", model: "google/gemini-3.8-flash",
    keyUrl: "https://openrouter.ai/keys" },
];

const sameUrl = (a, b) => (a ?? "").trim().replace(/\/+$/, "") === b.replace(/\/+$/, "");
// Same normalization as settings.service_id: each service keeps its own key.
const serviceId = (url) => (url ?? "").trim().replace(/\/+$/, "").toLowerCase();

// label / help: translation keys
const KEYS = {
  gemini: [{ field: "gemini_api_key", label: "admin.access.geminiKey", help: "admin.access.geminiHelp" }],
  anthropic: [{ field: "anthropic_api_key", label: "admin.access.anthropicKey", help: "admin.access.anthropicHelp" }],
  openai: [{ field: "openai_api_key", label: "admin.access.openaiKey", help: "" }],
};

const EDITABLE = ["llm", "model", "fallback_models", "openai_base_url", "tts_rate", "ankiconnect_url", "anki_sync",
                  "instructions", "profile_instructions", "picture_model"];

function session(action, value) {
  try {
    if (action === "get") return sessionStorage.getItem(PASSWORD_KEY);
    if (action === "set") sessionStorage.setItem(PASSWORD_KEY, value);
    if (action === "remove") sessionStorage.removeItem(PASSWORD_KEY);
  } catch {}
  return null;
}

document.addEventListener("alpine:init", () => {
  Alpine.data("admin", () => ({
    providers: PROVIDERS,
    services: SERVICES,
    loadedModels: [],    // models listed by the OpenAI-compatible service
    modelsInfo: "",
    loadingModels: false,
    status: null,
    password: null,
    unlocked: false,
    saved: null,     // settings as returned by the server (keys masked)
    form: null,      // editable copy of the non-secret settings
    keys: {},        // key field → new value typed ("" = unchanged, null = clear)
    pw: { login: "", new: "", confirm: "" },
    saving: false,
    testing: false,
    testResult: null,
    testingAnki: false,
    ankiResult: null,
    lessons: null,   // { lessons, profiles } from /api/admin/lessons (profiles: null = Anki closed)
    ankiStatus: null,  // /api/anki/status: profile, logged in to AnkiWeb (sync: null = unknown)
    saveState: "saved",  // "saved", "pending", "saving", "error": settings are saved as they change
    saveTimer: null,
    error: "",
    notice: "",

    async init() {
      await i18nReady;
      const setTitle = () => { document.title = `Cartable · ${t("admin.title")}`; };
      setTitle();
      document.addEventListener("i18n:changed", setTitle);
      try {
        this.status = await (await fetch("/api/admin")).json();
      } catch {
        this.error = t("errors.unreachable");
        return;
      }
      if (!this.status.allowed) return;  // Anki add-on, opened from a phone: see the message
      // Settings are saved as they change, after a short pause (a model name being typed
      // isn't saved half-way); API keys only once their field is left (see commitKey).
      this.$watch("form", () => {
        if (this.dirty({ keys: false })) this.scheduleSave();
      });
      document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "hidden" && this.saveTimer) this.flush();
      });
      if (!this.status.password_needed) return this.load(null);  // add-on, on the computer
      const remembered = session("get");
      if (this.status.password_set && remembered) await this.load(remembered);
      this.$nextTick(() => this.$refs.login?.focus());
    },

    async request(path, options = {}) {
      const res = await fetch(path, {
        ...options,
        headers: { "Content-Type": "application/json", "X-Admin-Password": this.password ?? "", ...options.headers },
      });
      if (res.status === 401 && path !== "/api/admin/password") {
        this.lock();
        throw new Error(t("errors.admin.wrong_password"));
      }
      if (!res.ok) {
        let detail = res.statusText;
        try { detail = (await res.json()).detail ?? detail; } catch {}
        throw new Error(errorMessage(detail));
      }
      return res.status === 204 ? null : res.json();
    },

    async load(password) {
      this.password = password;
      try {
        this.show(await this.request("/api/admin/settings"));
        this.unlocked = true;
        if (password) session("set", password);
        this.loadLessons();
        this.loadAnkiStatus();
      } catch (e) {
        this.error = e.message;
      }
    },

    show(saved) {
      this.saved = saved;
      // A copy: editing the form (profile_instructions is an object) mustn't change `saved`
      this.form = structuredClone(Object.fromEntries(EDITABLE.map((k) => [k, saved[k]])));
      this.keys = {};
    },

    unlock() {
      this.error = "";
      return this.load(this.pw.login);
    },

    lock() {
      session("remove");
      Object.assign(this, { unlocked: false, password: null, form: null, saved: null, testResult: null });
      this.pw.login = "";
    },

    // The demo provider (canned cards, whatever the photo) is for tests and development
    // (CARTABLE_LLM=fake): only listed when it is the saved choice.
    shownProviders() {
      return PROVIDERS.filter((p) => p.id !== "fake" || this.saved?.llm === "fake");
    },

    provider() {
      return PROVIDERS.find((p) => p.id === this.form.llm) ?? PROVIDERS[0];
    },

    // A model belongs to its provider: switching provider picks that provider's
    // model (the saved one when coming back to it, else its default / the
    // service's suggestion) instead of keeping e.g. "claude-opus-5" for OpenAI.
    providerChanged() {
      if (this.form.llm === this.saved.llm) this.form.model = this.saved.model;
      else this.form.model = this.form.llm === "openai" ? (this.service()?.model ?? "") : "";
      this.loadedModels = [];
      this.modelsInfo = "";
    },

    modelSuggestions() {
      if (this.form.llm !== "openai") return this.provider().models;
      const preset = this.service()?.model;
      return [...new Set([...(preset ? [preset] : []), ...this.loadedModels])];
    },

    // Service matching the saved/typed address, null for "Other".
    service() {
      return SERVICES.find((s) => sameUrl(this.form.openai_base_url, s.url)) ?? null;
    },

    chooseService(s) {
      this.form.openai_base_url = s ? s.url : "";
      this.form.model = s ? s.model : "";
      delete this.keys.openai_api_key;  // a key typed for the previous service isn't for this one
      this.loadedModels = [];
      this.modelsInfo = "";
    },

    // Saved key (masked) for a key field; for the OpenAI-compatible provider,
    // the key of the service currently chosen (or the .env default).
    savedKey(field) {
      if (field !== "openai_api_key") return this.saved[field];
      return this.saved.openai_keys?.[serviceId(this.form.openai_base_url)] || this.saved.openai_default_key;
    },

    async loadModels() {
      this.modelsInfo = "";
      if (!(await this.flush())) return;  // the server lists with the saved address and key
      this.loadingModels = true;
      try {
        const { models, vision_only } = await this.request("/api/admin/models", { method: "POST" });
        this.loadedModels = models;
        // vision_only: the service says which models accept images; otherwise the test tells.
        const key = vision_only ? "admin.service.modelsVision" : "admin.service.modelsAll";
        this.modelsInfo = models.length ? t(key, { count: models.length })
          : t(vision_only ? "admin.service.noVisionModels" : "admin.service.noModels");
      } catch (e) {
        this.modelsInfo = `✗ ${e.message}`;
      } finally {
        this.loadingModels = false;
      }
    },

    keysForProvider() {
      return KEYS[this.form.llm] ?? [];
    },

    clearKey(field) {
      this.keys[field] = null;
    },

    // What differs from the saved settings. `keys: false` leaves out API keys being
    // typed (a cleared key is always in: the 🗑 button is a deliberate action).
    changes({ keys = true } = {}) {
      const changes = {};
      const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
      for (const k of EDITABLE) if (!same(this.form[k], this.saved[k])) changes[k] = this.form[k];
      for (const [k, v] of Object.entries(this.keys)) {
        if (v === null) changes[k] = "";                        // clear
        else if (keys && v.trim()) changes[k] = v.trim();       // replace
      }
      return changes;
    },

    dirty(options) {
      return Boolean(this.form) && Object.keys(this.changes(options)).length > 0;
    },

    scheduleSave(delay = 900) {
      this.saveState = "pending";
      clearTimeout(this.saveTimer);
      this.saveTimer = setTimeout(() => this.flush({ keys: false }), delay);
    },

    // Save now what changed (with the typed keys unless `keys: false`).
    async flush({ keys = true } = {}) {
      clearTimeout(this.saveTimer);
      this.saveTimer = null;
      if (!this.dirty({ keys })) {
        if (this.saveState !== "error") this.saveState = "saved";
        return true;
      }
      return this.save({ keys });
    },

    // An API key is saved when its field is left (or Enter), never half-typed.
    commitKey(field) {
      if (this.keys[field]?.trim()) this.flush();
    },

    async save({ keys = true } = {}) {
      this.error = "";
      this.saving = true;
      this.saveState = "saving";
      const typed = { ...this.keys };
      try {
        const sent = this.changes({ keys });
        this.show(await this.request("/api/admin/settings", { method: "PUT", body: JSON.stringify(sent) }));
        if (!keys) {  // keys still being typed stay in their fields
          for (const [k, v] of Object.entries(typed)) if (v !== null && !(k in sent)) this.keys[k] = v;
        }
        this.saveState = this.dirty({ keys: false }) ? "pending" : "saved";
        return true;
      } catch (e) {
        this.error = e.message;  // e.g. an address without http://: not saved, said here
        this.saveState = "error";
        return false;
      } finally {
        this.saving = false;
      }
    },

    async test() {
      this.testResult = null;
      if (!(await this.flush())) return;  // tests what is shown
      this.testing = true;
      try {
        const r = await this.request("/api/admin/test", { method: "POST" });
        // Cartable needs both: reading the lesson photo and answering in JSON.
        const key = r.refused ? "admin.access.testRefused"
          : !r.json ? "admin.access.testNoJson"
          : !r.vision ? "admin.access.testNoVision" : "admin.access.testOk";
        this.testResult = { ok: Boolean(r.json && r.vision), text: t(key, r) };
      } catch (e) {
        this.testResult = { ok: false, text: `✗ ${e.message}` };
      } finally {
        this.testing = false;
      }
    },

    async loadAnkiStatus() {
      try {
        this.ankiStatus = await (await fetch("/api/anki/status")).json();
      } catch {
        this.ankiStatus = null;
      }
    },

    // In the add-on, Cartable talks to its own bridge (which mimics AnkiConnect):
    // don't mention AnkiConnect there. The open profile tells it's the right one.
    ankiOk(r) {
      if (!r.profile) return t("admin.anki.testNoProfile");
      return t(this.saved.embedded ? "admin.anki.testOkAddon" : "admin.anki.testOk", r);
    },

    async testAnki() {
      this.ankiResult = null;
      if (!(await this.flush())) return;
      this.testingAnki = true;
      try {
        const r = await (await fetch("/api/anki/status")).json();
        this.ankiStatus = r;
        this.ankiResult = r.available
          ? { ok: true, text: this.ankiOk(r) }
          : { ok: false, text: `✗ ${errorMessage(r.error)}` };
      } catch {
        this.ankiResult = { ok: false, text: `✗ ${t("errors.unreachable")}` };
      } finally {
        this.testingAnki = false;
      }
    },

    // Anki's profiles, and those that have instructions but aren't in Anki any more
    instructionProfiles() {
      const fromAnki = this.lessons?.profiles ?? [];
      const saved = Object.keys(this.form?.profile_instructions ?? {});
      return [...new Set([...fromAnki, ...saved])];
    },

    // --- Lessons: who they belong to ------------------------------------------
    async loadLessons() {
      try {
        this.lessons = await this.request("/api/admin/lessons");
      } catch (e) {
        this.error = e.message;
      }
    },

    // Owner not among Anki's profiles (renamed or deleted): nobody can change the lesson.
    ownerMissing(l) {
      return Boolean(l.owner && this.lessons.profiles && !this.lessons.profiles.includes(l.owner));
    },

    ownerChoices(l) {
      const profiles = this.lessons.profiles ?? [];
      return l.owner && !profiles.includes(l.owner) ? [l.owner, ...profiles] : profiles;
    },

    async setAccess(l, changes) {
      this.error = this.notice = "";
      try {
        Object.assign(l, await this.request(`/api/admin/lessons/${l.id}`, {
          method: "PUT", body: JSON.stringify(changes),
        }));
        this.notice = t("admin.lessons.changed", { deck: l.deck });
      } catch (e) {
        this.error = e.message;
        await this.loadLessons();  // show what is really saved
      }
    },

    async deleteLesson(l) {
      if (!confirm(t("admin.lessons.confirmDelete", { deck: l.deck }))) return;
      this.error = this.notice = "";
      try {
        await this.request(`/api/admin/lessons/${l.id}`, { method: "DELETE" });
        this.lessons.lessons = this.lessons.lessons.filter((x) => x.id !== l.id);
      } catch (e) {
        this.error = e.message;
      }
    },

    // --- AI costs (US dollars) --------------------------------------------
    formatCost(usd) {
      Alpine.store("i18n").version;
      if (usd === null || usd === undefined) return "—";
      if (usd < 1) {  // AI calls cost cents: "2.7 ¢" says more than "$0.03"
        const cents = (usd * 100).toLocaleString(I18N.lang, { maximumSignificantDigits: 2 });
        return t("admin.costs.cents", { cents });  // US cents: "¢" alone reads as euro cents
      }
      return usd.toLocaleString(I18N.lang, { style: "currency", currency: "USD" });
    },

    lessonCost(l) {
      const known = l.ai_calls.filter((c) => c.cost !== null);
      return known.length ? known.reduce((sum, c) => sum + c.cost, 0) : null;
    },

    // "1.4 ¢ · gemini-3.8-flash · 1 generation + 2 corrections"
    costLine(l) {
      const models = [...new Set(l.ai_calls.map((c) => c.model))].join(", ");
      const count = (kind) => l.ai_calls.filter((c) => c.kind === kind).length;
      const [extract, revise, picture] = [count("extract"), count("revise"), count("picture")];
      const estimate = l.ai_calls.some((c) => !c.exact && c.cost) ? "≈ " : "";
      const counts = [t("admin.costs.extracts", { count: extract })];
      if (revise) counts.push(t("admin.costs.revisions", { count: revise }));
      if (picture) counts.push(t("admin.costs.pictures", { count: picture }));
      return `💰 ${estimate}${this.formatCost(this.lessonCost(l))} · ${models} · ${counts.join(" + ")}`;
    },

    callLine(call) {
      const tokens = call.input_tokens === null ? "" : ` · ${t("admin.costs.tokens", {
        input: call.input_tokens.toLocaleString(I18N.lang), output: (call.output_tokens ?? 0).toLocaleString(I18N.lang),
      })}`;
      const cost = call.cost === null ? t("admin.costs.unknown") : (call.exact ? "" : "≈ ") + this.formatCost(call.cost);
      return `${this.formatDate(call.at)} · ${t(`admin.costs.kind.${call.kind}`)} · ${call.provider} · ${call.model}${tokens} · ${cost}`;
    },

    formatDate(iso) {
      Alpine.store("i18n").version;  // re-render when the language changes
      return new Date(iso).toLocaleDateString(I18N.lang, { day: "numeric", month: "short", year: "numeric" });
    },

    async setPassword(current) {
      if (this.pw.new !== this.pw.confirm) {
        this.error = t("admin.mismatch");
        return false;
      }
      try {
        await this.request("/api/admin/password", {
          method: "POST",
          body: JSON.stringify({ current, new: this.pw.new }),
        });
      } catch (e) {
        this.error = e.message;
        return false;
      }
      const password = this.pw.new;
      this.pw.new = this.pw.confirm = "";
      return password;
    },

    async createPassword() {
      this.error = "";
      const password = await this.setPassword(null);
      if (!password) return;
      this.status.password_set = true;
      await this.load(password);
    },

    async changePassword() {
      this.error = "";
      const password = await this.setPassword(this.password);
      if (!password) return;
      this.password = password;
      session("set", password);
      this.notice = t("admin.changed");
    },
  }));
});
