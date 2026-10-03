const PASSWORD_KEY = "notosaurus.admin";  // kept for the browser session only

// Names and descriptions: admin.provider.<id>.* in static/i18n/<lang>.json
const PROVIDERS = [
  { id: "gemini", models: ["gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.1-pro-preview"] },
  { id: "anthropic", models: ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"] },
  { id: "openai", models: ["gpt-6-luna", "gpt-6-sol", "gpt-6-astra"] },
  { id: "openrouter", models: [] },  // its short list, loaded from OpenRouter
  { id: "compatible", models: ["qwen2.5vl", "gemma3"] },
  { id: "fake", models: [] },
];
// Speak OpenAI's API: their models can be listed from the service
const OPENAI_LIKE = ["openai", "openrouter", "compatible"];

// label / help: translation keys; keyUrl: where to create the key
const KEYS = {
  gemini: [{ field: "gemini_api_key", label: "admin.access.geminiKey", help: "admin.access.geminiHelp" }],
  anthropic: [{ field: "anthropic_api_key", label: "admin.access.anthropicKey", help: "admin.access.anthropicHelp" }],
  openai: [{ field: "openai_api_key", label: "admin.access.openaiKey", keyUrl: "https://platform.openai.com/api-keys" }],
  openrouter: [{ field: "openrouter_api_key", label: "admin.access.openrouterKey", keyUrl: "https://openrouter.ai/keys" }],
  compatible: [{ field: "compatible_api_key", label: "admin.access.compatibleKey" }],
};

// Card pictures: the services that draw, the models suggested in each
const PICTURE_SERVICES = ["gemini", "openai", "openrouter"];
const PICTURE_MODELS = {
  gemini: ["gemini-3.1-flash-lite-image", "gemini-3.1-flash-image"],
  openai: ["gpt-image-1-mini", "gpt-image-1"],
  openrouter: ["google/gemini-3.1-flash-lite-image", "google/gemini-3.1-flash-image", "openai/gpt-5-image-mini"],
};
const PICTURE_KEYS = { gemini: "gemini_api_key", openai: "openai_api_key", openrouter: "openrouter_api_key" };

const EDITABLE = ["llm", "model", "fallback_models", "compatible_base_url", "picture_service", "tts_rate", "ankiconnect_url", "anki_sync",
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
    pictureServices: PICTURE_SERVICES,
    PICTURE_MODELS,
    loadedModels: [],    // models listed by the OpenAI-like service
    modelAliases: [],    // OpenRouter: "~…-latest", the latest model of each main family (the short list)
    modelNames: {},      // id → name given by the service ("Google: Gemini Flash Latest")
    recommendedModel: null,
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
    phone: null,      // { url }: the link in the phones' QR code

    async init() {
      await i18nReady;
      const setTitle = () => { document.title = `Notosaurus · ${t("admin.title")}`; };
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

    async loadPhone() {
      try {
        this.phone = await this.request("/api/admin/phone");
      } catch {}  // the section stays hidden
    },

    // A new token: every phone must scan the QR code again (one was lost or lent)
    async unpairPhones() {
      if (!confirm(t("admin.phones.confirmUnpair"))) return;
      try {
        this.phone = await this.request("/api/admin/phone/unpair", { method: "POST" });
        this.notice = t("admin.phones.unpaired");
      } catch (e) {
        this.error = e.message;
      }
    },

    async load(password) {
      this.password = password;
      try {
        this.show(await this.request("/api/admin/settings"));
        this.unlocked = true;
        if (password) session("set", password);
        this.loadLessons();
        this.loadAnkiStatus();
        this.loadPhone();
        this.autoLoadModels();
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
    // (NOTOSAURUS_LLM=fake): only listed when it is the saved choice.
    shownProviders() {
      return PROVIDERS.filter((p) => p.id !== "fake" || this.saved?.llm === "fake");
    },

    provider() {
      return PROVIDERS.find((p) => p.id === this.form.llm) ?? PROVIDERS[0];
    },

    // A model belongs to its provider: switching provider takes the saved one when
    // coming back to it, else the provider's default (shown in the field) instead of
    // keeping e.g. "claude-opus-5" for OpenAI.
    providerChanged() {
      this.form.model = this.form.llm === this.saved.llm ? this.saved.model : "";
      this.clearModels();
      this.autoLoadModels();
    },

    openaiLike() {
      return OPENAI_LIKE.includes(this.form.llm);
    },

    // Once the access is set (key, or address), the service's models list themselves:
    // OpenRouter's short list shows without a click (listing models is free).
    accessReady() {
      const ready = {
        openai: this.saved.openai_api_key,
        openrouter: this.saved.openrouter_api_key,
        compatible: this.saved.compatible_base_url,
      };
      return Boolean(ready[this.form.llm]) && this.form.llm === this.saved.llm;
    },

    autoLoadModels() {
      if (this.openaiLike() && this.accessReady() && !this.loadedModels.length && !this.loadingModels) this.loadModels();
    },

    clearModels() {
      Object.assign(this, { loadedModels: [], modelAliases: [], modelNames: {}, recommendedModel: null });
      this.modelsInfo = "";
    },

    // Every model the service lists: the field filters them as one types (OpenRouter's
    // suggested ones are the buttons below it)
    modelSuggestions() {
      if (!this.openaiLike()) return this.provider().models;
      return [...new Set([...this.provider().models, ...this.modelAliases, ...this.loadedModels])];
    },

    // "Google: Gemini Flash Latest" → "Gemini Flash"
    modelLabel(id) {
      const name = this.modelNames[id] ?? id.replace(/^~[^/]+\//, "");
      return name.replace(/^[^:]+:\s*/, "").replace(/\s+latest$/i, "").replace(/-latest$/, "");
    },

    // --- Card pictures: who draws them (same rule as pictures.service on the server)
    hasKey(service) {
      return Boolean(this.saved[PICTURE_KEYS[service]]);
    },

    drawingService() {
      const chosen = this.form.picture_service;
      if (chosen === "none") return "";
      if (chosen) return this.hasKey(chosen) ? chosen : "";
      if (PICTURE_SERVICES.includes(this.form.llm)) return this.hasKey(this.form.llm) ? this.form.llm : "";
      return ["openrouter", "gemini", "openai"].find((s) => this.hasKey(s)) ?? "";
    },

    setPictureService(service) {
      if (service !== this.form.picture_service) this.form.picture_model = "";  // a model belongs to its service
      this.form.picture_service = service;
    },

    pictureSummary() {
      const name = (id) => t(`admin.provider.${id}.name`);
      const drawing = this.drawingService();
      const chosen = this.form.picture_service;
      if (chosen === "none") return t("admin.pictures.noneChosen");
      if (!drawing) return chosen ? t("admin.pictures.missingKey", { service: name(chosen) }) : t("admin.pictures.nobody", { cards: name(this.form.llm) });
      if (chosen) return t("admin.pictures.drawnBy", { service: name(drawing) });
      if (drawing === this.form.llm) return t("admin.pictures.drawnBySame", { service: name(drawing) });
      return t("admin.pictures.cantDraw", { cards: name(this.form.llm), service: name(drawing) });
    },

    // Saved key (masked) for a key field
    savedKey(field) {
      return this.saved[field];
    },

    async loadModels() {
      this.modelsInfo = "";
      if (!(await this.flush())) return;  // the server lists with the saved address and key
      this.loadingModels = true;
      try {
        const { models, vision_only, aliases = [], names = {}, recommended = null } =
          await this.request("/api/admin/models", { method: "POST" });
        Object.assign(this, { loadedModels: models, modelAliases: aliases, modelNames: names, recommendedModel: recommended });
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
        this.autoLoadModels();  // a key or an address just saved
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
        // Notosaurus needs both: reading the lesson photo and answering in JSON.
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

    // In the add-on, Notosaurus talks to its own bridge (which mimics AnkiConnect):
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

    // Its cards in Anki go only when confirmed (asked when the open profile has some)
    async deleteLesson(l) {
      if (!confirm(t("admin.lessons.confirmDelete", { deck: l.deck }))) return;
      this.error = this.notice = "";
      try {
        let count = 0;
        try {
          count = (await this.request(`/api/admin/lessons/${l.id}/anki-notes`)).count;
        } catch {}  // Anki unknown: the lesson alone
        const anki = count > 0 && confirm(t("admin.lessons.alsoAnki", { count }));
        const r = await this.request(`/api/admin/lessons/${l.id}?anki=${anki}`, { method: "DELETE" });
        if (r.anki_deleted) this.notice = t("app.lessons.deletedInAnki", { count: r.anki_deleted });
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
