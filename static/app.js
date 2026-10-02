const MAX_SIDE = 1600;   // px: enough to read a page, light to upload
const JPEG_QUALITY = 0.85;
const LAST_PROMPT = "cartable.prompt";

// Case- and accent-insensitive form, for search.
const normalize = (text) => text.normalize("NFD").replace(/\p{Diacritic}/gu, "").toLowerCase();

// Shrinks a phone photo (3–5 MB) to ~1600 px JPEG before upload.
async function resize(file) {
  const bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
  const scale = Math.min(1, MAX_SIDE / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(bitmap.width * scale);
  canvas.height = Math.round(bitmap.height * scale);
  canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  bitmap.close();
  return new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", JPEG_QUALITY));
}

// A photo turned a quarter turn clockwise (JPEG).
async function rotateBlob(blob) {
  const bitmap = await createImageBitmap(blob);
  const canvas = document.createElement("canvas");
  canvas.width = bitmap.height;
  canvas.height = bitmap.width;
  const ctx = canvas.getContext("2d");
  ctx.translate(canvas.width, 0);
  ctx.rotate(Math.PI / 2);
  ctx.drawImage(bitmap, 0, 0);
  bitmap.close();
  return new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", JPEG_QUALITY));
}

async function api(path, options = {}) {
  // The server uses the page's language for default prompts and AI summaries.
  const headers = { "X-Cartable-Lang": I18N.lang, ...options.headers };
  const res = await fetch(path, { ...options, headers });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail ?? detail; } catch {}
    const error = new Error(errorMessage(detail));
    Object.assign(error, { status: res.status, detail });
    throw error;
  }
  return res;
}

function storage(action, value) {
  try {
    if (action === "get") return localStorage.getItem(LAST_PROMPT);
    localStorage.setItem(LAST_PROMPT, value);
  } catch {}
}

const RECENT_PROMPTS = 4;  // chips shown before "All"
const SAVE_DELAY = 800;  // ms: save shortly after the last edit
const PROFILE_POLL = 3000;  // ms: follow Anki profile switches (local request, only while visible)

const formatDate = (iso) =>
  new Date(iso).toLocaleString(I18N.lang, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

// Formulas: MathJax syntax, \( … \) within text, \[ … \] on their own (as in Anki)
const MATH = /\\\((.+?)\\\)|\\\[(.+?)\\\]/gs;
const HAS_MATH = /\\\(|\\\[/;  // as app/tts.py: no read-aloud for these
const CLOZE = /\{\{c\d+::/;  // a gap, as app/anki.py: {{c1::1789}}
// Outside formulas: a gap's start ({{c2::) and its end, with an optional hint (::lieu}})
const GAP_START = /\{\{c(\d+)::/g;
const GAP_END = /(?:::[^{}]*?)?\}\}/g;
const escapeHtml = (text) => text.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

let nextKey = 0;
const withKey = (card) => ({ info: "", subdeck: "", tags: [], picture: "", picture_prompt: "", ...card, key: nextKey++ });

// A card's stable id (crypto.randomUUID needs HTTPS; getRandomValues doesn't)
const newId = () => [...crypto.getRandomValues(new Uint8Array(6))].map((b) => b.toString(16).padStart(2, "0")).join("");

document.addEventListener("alpine:init", () => {
  Alpine.data("cartable", () => ({
    photos: [],          // { blob, url }
    prompts: [],
    selectedId: null,
    form: { name: "", text: "", deck: "", voice: "", typing: false, dictation: false },
    voices: [],          // edge-tts voices: { voice, locale, gender }
    deck: "",
    cards: [],
    reverse: false,
    decks: [],                   // existing deck names, for the deck field
    lessonChoice: "",            // what the AI chose to make (prompt "Automatic")
    lessonExported: false,       // already sent to Anki or exported
    photosEdited: false,         // photos added or removed since the lesson was shown
    typing: false,               // the answer is typed in Anki
    dictation: false,            // a dictation card: hear the back, type it
    lessons: [],         // saved lesson summaries
    lessonId: null,       // open lesson (null = new lesson, not generated yet)
    saveState: "",       // "", "pending", "saving", "saved", "error"
    lastSaved: "",
    saveTimer: null,
    loading: false,
    exporting: false,
    lessonsOpen: false,
    picker: { open: false, query: "" },
    // Natural-language correction of the cards; `undo` holds the previous version.
    revision: { text: "", busy: false, summary: "", stats: "", undo: null },
    removal: { open: false, lesson: null, checking: false, available: false, count: 0, anki: false, busy: false },
    editor: { open: false, id: null, name: "", text: "", deck: "", voice: "", error: "" },
    error: "",
    success: "",
    anki: { available: false },  // Anki reachable → direct send; `profile`: open Anki profile
    settingsHere: true,          // false on a phone when Cartable runs in the Anki add-on
    profileToApply: null,        // Anki profile switch waiting for the current task to finish
    diagramWarning: false,       // the AI model places diagram masks loosely: say so
    lessonOwner: "",              // Anki profile that created the open lesson ("" = nobody: shared)
    lessonShared: false,
    lessonPrompt: "",            // prompt text the open lesson was generated with
    frames: [],                  // diagram frames of the open lesson: what Anki shows of each photo          // visible from every profile (only the owner's profile can change it)
    sending: false,
    drawing: false,              // the pictures the cards ask for are being drawn
    pictureJobs: 0,              // a card's picture being redrawn, uploaded or removed
    katexReady: false,           // the formula previews can be drawn (KaTeX loaded)

    async init() {
      // Any change to the open lesson is saved automatically.
      Alpine.effect(() => {
        const snapshot = this.snapshot();
        if (this.lessonId && !this.readOnly() && snapshot !== this.lastSaved) this.scheduleSave();
      });
      // Phone locked or tab closed: don't wait for the delay.
      document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "hidden" && this.saveTimer) this.saveNow(true);
      });

      // KaTeX loads after the page (deferred): the previews redraw once it's there
      const katexLoaded = () => { this.katexReady = typeof katex !== "undefined"; };
      if (document.readyState === "complete") katexLoaded();
      else window.addEventListener("load", katexLoaded);

      await i18nReady;  // the language is needed for the first default prompts
      try {
        this.settingsHere = (await (await fetch("/api/admin")).json()).allowed;
        const config = await (await api("/api/config")).json();
        this.diagramWarning = config.diagram_warning;
      } catch {}
      await Promise.all([this.loadPrompts(storage("get")), this.loadLessons()]);
      this.checkAnki();
      // Anki may be started later: check again when coming back to the app.
      document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible") this.checkAnki();
      });
      // Follow profile switches in Anki while the page stays open.
      setInterval(() => { if (document.visibilityState === "visible") this.checkAnki(); }, PROFILE_POLL);
      try {
        this.voices = await (await api("/api/voices")).json();
      } catch {}  // the list is only an input aid
    },

    // --- Photos ---------------------------------------------------------
    async addPhotos(event) {
      this.error = "";
      for (const file of event.target.files) {
        try {
          const blob = await resize(file);
          this.photos.push({ blob, url: URL.createObjectURL(blob) });
          this.photosEdited = Boolean(this.lessonId);
        } catch {
          this.error = t("app.photos.unreadable", { name: file.name });
        }
      }
      event.target.value = "";  // allows picking the same photo again
    },

    // A quarter turn clockwise, when a photo (or the AI's guess) is sideways. In a saved
    // lesson the server turns the photo and its diagram masks; before, just the photo here.
    async rotatePhoto(i) {
      this.error = "";
      try {
        if (!this.lessonId) {
          const blob = await rotateBlob(this.photos[i].blob);
          URL.revokeObjectURL(this.photos[i].url);
          this.photos.splice(i, 1, { blob, url: URL.createObjectURL(blob) });
          return;
        }
        if (this.saveTimer) await this.saveNow();
        const lesson = await (await api(`/api/lessons/${this.lessonId}/photos/${i + 1}/rotate`, { method: "POST" })).json();
        await this.loadPhotos(lesson);
        lesson.cards.forEach((card, n) => { if (this.cards[n]) this.cards[n].mask = card.mask; });
        this.frames = lesson.frames ?? [];
        this.lastSaved = this.snapshot();  // already saved by the server
      } catch (e) {
        this.error = e.message;
      }
    },

    removePhoto(i) {
      URL.revokeObjectURL(this.photos[i].url);
      this.photos.splice(i, 1);
      this.photosEdited = Boolean(this.lessonId);
    },

    // --- Prompts ---------------------------------------------------------
    async loadPrompts(selectId) {
      try {
        this.prompts = await (await api("/api/prompts")).json();
      } catch (e) {
        this.error = t("app.prompt.unavailable", { message: e.message });
        return;
      }
      // Ids: the user's are numbers, Cartable's "cartable:…" (stored as text in the browser)
      const found = this.prompts.find((c) => String(c.id) === String(selectId)) ?? this.prompts[0];
      this.selectedId = found?.id ?? null;
      this.selectPrompt();
    },

    current() {
      return this.prompts.find((c) => c.id === this.selectedId);
    },

    // Most recently used prompts first (never used: oldest first), always
    // including the selected one.
    recentPrompts() {
      const byUse = [...this.prompts].sort(
        (a, b) => (b.used_at ?? "").localeCompare(a.used_at ?? "") || a.id - b.id);
      const recent = byUse.slice(0, RECENT_PROMPTS);
      const selected = this.current();
      if (selected && !recent.includes(selected)) recent.splice(RECENT_PROMPTS - 1, 1, selected);
      return recent;
    },

    filteredPrompts() {
      const words = normalize(this.picker.query).split(/\s+/).filter(Boolean);
      return this.prompts
        .filter((c) => words.every((w) => normalize(`${c.name} ${c.text}`).includes(w)))
        .sort((a, b) => a.name.localeCompare(b.name, I18N.lang, { sensitivity: "base" }));
    },

    openPicker() {
      this.picker = { open: true, query: "" };
      this.$nextTick(() => this.$refs.pickerSearch.focus());
    },

    choose(id) {
      this.selectedId = id;
      this.selectPrompt();
      this.picker.open = false;
    },

    selectPrompt() {
      const c = this.current();
      this.form = c
        ? { name: c.name, text: c.text, deck: c.deck, voice: c.voice, typing: c.typing, dictation: c.dictation }
        : { name: "", text: "", deck: "", voice: "", typing: false, dictation: false };
      if (c) storage("set", c.id);
    },

    // Text differing from the selected saved prompt (or no prompt selected, e.g. a
    // reopened lesson's own prompt): it can become a new saved prompt.
    canSaveAsNew() {
      const c = this.current();
      return this.form.text.trim() !== "" && (!c || c.text !== this.form.text);
    },

    isModified() {
      const c = this.current();
      return c && c.text !== this.form.text;
    },

    // Editor sheet: "new" = empty form, "edit" = the selected prompt,
    // "copy" = a new prompt starting from the text tweaked for this time.
    openEditor(mode) {
      const c = this.current();
      const base = mode === "new"
        ? { name: "", text: "", deck: "", voice: c?.voice ?? "", typing: false, dictation: false }
        : { ...this.form };
      if (mode === "copy") base.name = "";
      // Cartable's prompts open read-only: "Duplicate" makes a copy to change
      const builtin = mode === "edit" && Boolean(c?.builtin);
      if (builtin) {
        Object.assign(base, { name: c.name, text: c.text, deck: c.deck, voice: c.voice, typing: c.typing, dictation: c.dictation });
      }
      // Voice of the backs: none, automatic (the backs' language, found by the AI), or a chosen one
      const voiceMode = !base.voice ? "none" : base.voice === "auto" ? "auto" : "pick";
      this.editor = { open: true, id: mode === "edit" ? c.id : null, builtin, error: "", ...base, voiceMode };
      this.$nextTick(() => {
        if (!this.editor.name) this.$refs.editorName.focus();
      });
    },

    setVoiceMode(mode) {
      if (this.editor.builtin) return;
      if (mode === "pick" && ["", "auto"].includes(this.editor.voice)) this.editor.voice = "";
      this.editor.voiceMode = mode;
    },

    async saveEditor() {
      const { id, name, text, deck, voiceMode, typing, dictation } = this.editor;
      const voice = voiceMode === "none" ? "" : voiceMode === "auto" ? "auto" : this.editor.voice;
      if (!name.trim() || !text.trim()) {
        this.editor.error = t("app.editor.required");
        return;
      }
      try {
        const saved = await (await api(id ? `/api/prompts/${id}` : "/api/prompts", {
          method: id ? "PUT" : "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            name: name.trim(), text: text.trim(), deck: deck.trim(), voice: voice.trim(),
            typing: Boolean(typing), dictation: Boolean(dictation && voice.trim()),
          }),
        })).json();
        this.editor.open = false;
        await this.loadPrompts(saved.id);
      } catch (e) {
        this.editor.error = t("common.failed", { message: e.message });
      }
    },

    // A copy of any prompt (Cartable's included), opened in the editor to be changed.
    async duplicatePrompt(prompt) {
      try {
        const copy = await (await api(`/api/prompts/${encodeURIComponent(prompt.id)}/duplicate`, { method: "POST" })).json();
        this.picker.open = false;
        await this.loadPrompts(copy.id);
        this.openEditor("edit");
      } catch (e) {
        this.error = t("common.failed", { message: e.message });
      }
    },

    // From the editor (the prompt being edited) or from the list of all prompts.
    async deletePrompt(prompt = null) {
      const inEditor = prompt === null;
      const { id, name } = inEditor ? this.editor : prompt;
      if (!id || !confirm(t("app.editor.confirmDelete", { name }))) return;
      try {
        await api(`/api/prompts/${id}`, { method: "DELETE" });
        if (inEditor) this.editor.open = false;
        await this.loadPrompts(id === this.selectedId ? null : this.selectedId);
      } catch (e) {
        const message = t("common.failed", { message: e.message });
        if (inEditor) this.editor.error = message;
        else this.error = message;
      }
    },

    // --- Extraction -----------------------------------------------------
    // A lesson open and ours: generating again replaces its cards (no second lesson).
    // Someone else's: a new lesson of our own.
    regeneratesInPlace() {
      return Boolean(this.lessonId) && !this.readOnly();
    },

    async extract() {
      this.error = "";
      this.loading = true;
      if (this.saveTimer) await this.saveNow();
      const inPlace = this.regeneratesInPlace();
      // What "Undo" brings back; not when the photos changed (the old ones are gone)
      const before = inPlace && !this.photosEdited ? this.undoState() : null;
      const body = new FormData();
      this.photos.forEach((p, i) => body.append("images", p.blob, `page-${i + 1}.jpg`));
      body.append("prompt", this.form.text);
      body.append("deck", this.form.deck);
      body.append("voice", this.form.voice);
      body.append("typing", Boolean(this.form.typing));
      body.append("dictation", Boolean(this.form.dictation));
      if (this.selectedId) body.append("prompt_id", this.selectedId);
      try {
        const url = inPlace ? `/api/lessons/${this.lessonId}/regenerate` : "/api/extract";
        const lesson = await (await api(url, { method: "POST", body })).json();
        const used = this.current();
        if (used) used.used_at = new Date().toISOString();  // moves it to the front of the chips
        await this.loadPhotos(lesson);  // as saved: the server turns sideways photos upright
        this.show(lesson);
        if (inPlace) {
          this.revision = { text: "", busy: false, summary: t("app.prompt.regenerated"), stats: "", undo: before };
        }
        this.loadLessons();
        if (!this.cards.length) this.error = t("app.review.noCards");
        this.drawPictures();  // the cards show now, their pictures when drawn
      } catch (e) {
        this.error = e.message;
      } finally {
        this.loading = false;
      }
    },

    // Decks that already exist (Anki's, the lessons'), offered when the deck is edited
    async loadDecks() {
      try {
        this.decks = await (await api("/api/decks")).json();
      } catch {}  // only suggestions
    },

    // --- Saved lessons ---------------------------------------------------
    async loadLessons() {
      try {
        this.lessons = await (await api("/api/lessons")).json();
      } catch {}  // history is optional for creating a lesson
    },

    formatDate(iso) {
      Alpine.store("i18n").version;  // re-render dates when the language changes
      return formatDate(iso);
    },

    // Lessons of the open Anki profile, shared lessons, lessons without owner (the server
    // sends no others; filtered here too, so a profile switch shows at once).
    visibleLessons() {
      const profile = this.anki.profile;
      if (!profile) return this.lessons;
      return this.lessons.filter((l) => l.shared || !l.owner || l.owner === profile);
    },

    // Only the lesson's creator decides to share it
    canShare() {
      return Boolean(this.lessonOwner) && this.anki.profile === this.lessonOwner;
    },

    // Someone else's lesson (shared, or seen with "All profiles"): it can be read,
    // sent to Anki and exported, not changed. Lessons without owner are everyone's.
    readOnly() {
      return Boolean(this.lessonOwner) && this.anki.profile !== this.lessonOwner;
    },

    canDelete(l) {
      return !l.owner || l.owner === this.anki.profile;
    },

    // Shows a lesson coming from the server (fresh generation or reopened).
    show(lesson) {
      clearTimeout(this.saveTimer);
      this.saveTimer = null;
      this.lessonId = lesson.id;
      this.lessonOwner = lesson.owner ?? "";
      this.lessonShared = lesson.shared ?? false;
      this.lessonPrompt = lesson.prompt ?? "";
      this.lessonChoice = lesson.choice ?? "";
      this.lessonExported = Boolean(lesson.exported_at);
      this.photosEdited = false;
      this.frames = lesson.frames ?? [];
      this.deck = lesson.deck;
      this.cards = lesson.cards.map(withKey);
      this.reverse = lesson.reverse;
      this.typing = lesson.typing ?? false;
      this.dictation = lesson.dictation ?? false;
      this.form.voice = lesson.voice;
      this.lastSaved = this.snapshot();
      this.saveState = "saved";
      this.revision = { text: "", busy: false, summary: "", stats: "", undo: null };
      this.$nextTick(() => this.$refs.review?.scrollIntoView({ behavior: "smooth" }));
    },

    async openLesson(id) {
      this.error = "";
      if (this.saveTimer) await this.saveNow();
      try {
        const lesson = await (await api(`/api/lessons/${id}`)).json();
        await this.loadPhotos(lesson);
        this.show(lesson);
        this.usePromptOf(lesson);
      } catch (e) {
        this.error = t("app.lessons.openFailed", { message: e.message });
      }
    },

    // A reopened lesson brings back the prompt it was generated with, so "Generate
    // again" starts from it; the saved prompt with that exact text is selected, if any.
    usePromptOf(lesson) {
      if (!lesson.prompt) return;
      const saved = this.prompts.find((c) => c.text === lesson.prompt);
      this.selectedId = saved?.id ?? null;
      this.form = saved
        ? { name: saved.name, text: saved.text, deck: saved.deck, voice: lesson.voice, typing: saved.typing, dictation: saved.dictation }
        : { name: "", text: lesson.prompt, deck: "", voice: lesson.voice, typing: lesson.typing, dictation: lesson.dictation };
    },

    // The lesson's photos from the server, as blobs, so a generation can be run again.
    async loadPhotos(lesson) {
      const photos = [];
      for (let n = 1; n <= lesson.photo_count; n++) {
        const blob = await (await api(`/api/lessons/${lesson.id}/photos/${n}`)).blob();
        photos.push({ blob, url: URL.createObjectURL(blob) });
      }
      this.clearPhotos();
      this.photos = photos;
    },

    async newLesson() {
      if (this.saveTimer) await this.saveNow();
      this.clearPhotos();
      this.lessonId = null;
      this.lessonOwner = "";
      this.lessonShared = false;
      this.deck = "";
      this.cards = [];
      this.reverse = false;
      this.typing = false;
      this.dictation = false;
      this.saveState = "";
      this.lessonPrompt = "";
      this.lessonChoice = "";
      this.lessonExported = false;
      this.photosEdited = false;
      this.frames = [];
      // Back to the saved prompt picked last (a reopened lesson may have left its own text)
      if (!this.current()) {
        this.selectedId = (this.prompts.find((c) => String(c.id) === storage("get")) ?? this.prompts[0])?.id ?? null;
      }
      this.selectPrompt();  // restores the selected prompt's text and voice
      window.scrollTo({ top: 0, behavior: "smooth" });
    },

    // Asks first; its cards in Anki only go when ticked (their review history goes with them)
    async deleteLesson(l) {
      this.removal = { open: true, lesson: l, checking: true, available: false, count: 0, anki: false, busy: false };
      try {
        const found = await (await api(`/api/lessons/${l.id}/anki-notes`)).json();
        if (this.removal.lesson === l) Object.assign(this.removal, found);
      } catch {}  // Anki unknown: the lesson alone
      if (this.removal.lesson === l) this.removal.checking = false;
    },

    async confirmDeleteLesson() {
      const { lesson: l, anki } = this.removal;
      this.removal.busy = true;
      try {
        const r = await (await api(`/api/lessons/${l.id}?anki=${anki}`, { method: "DELETE" })).json();
        this.removal.open = false;
        if (r.anki_deleted) {
          this.success = t("app.lessons.deletedInAnki", { count: r.anki_deleted }) + (r.synced ? " " + t("app.send.synced") : "");
        }
        if (r.sync_error) this.error = t("app.lessons.deletedNoSync", { reason: errorMessage(r.sync_error) });
        if (l.id === this.lessonId) {
          clearTimeout(this.saveTimer);
          this.saveTimer = null;
          this.lessonId = null;
          await this.newLesson();
        }
        await this.loadLessons();
      } catch (e) {
        this.error = e.message;
      }
    },

    clearPhotos() {
      this.photos.forEach((p) => URL.revokeObjectURL(p.url));
      this.photos = [];
    },

    payload() {
      return {
        deck: this.deck,
        // Without the page's own fields: key, and _state, _panel, _subject, _drawing…
        cards: this.cards.map((card) =>
          Object.fromEntries(Object.entries(card).filter(([k]) => k !== "key" && !k.startsWith("_")))),
        voice: this.form.voice,
        reverse: this.reverse,
        typing: this.typing,
        dictation: this.dictation,
        shared: this.lessonShared,
        frames: this.frames,
        prompt: this.lessonPrompt,
        choice: this.lessonChoice,
      };
    },

    snapshot() {
      return JSON.stringify(this.payload());
    },

    scheduleSave() {
      this.saveState = "pending";
      clearTimeout(this.saveTimer);
      this.saveTimer = setTimeout(() => this.saveNow(), SAVE_DELAY);
    },

    async saveNow(keepalive = false) {
      clearTimeout(this.saveTimer);
      this.saveTimer = null;
      if (!this.lessonId || this.readOnly()) return;
      if (this.drawing || this.pictureJobs) return this.scheduleSave();  // after: the server is changing pictures
      const body = this.snapshot();
      // Before the request: otherwise clearing saveTimer re-runs the autosave effect,
      // which would see an unsaved snapshot and schedule the same save again.
      this.lastSaved = body;
      this.saveState = "saving";
      try {
        await api(`/api/lessons/${this.lessonId}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body,
          keepalive,  // lets the request complete even if the page closes
        });
        this.saveState = this.snapshot() === body ? "saved" : "pending";
        this.loadLessons();
      } catch (e) {
        if (e.detail?.code === "lesson.read_only") {
          // Another Anki profile was opened just before this save: show the saved lesson.
          await this.openLesson(this.lessonId);
          return;
        }
        this.saveState = "error";  // not retried in a loop; the next edit saves again
      }
    },

    // --- AI correction --------------------------------------------------
    async revise() {
      const instruction = this.revision.text.trim();
      if (!instruction || this.revision.busy || !this.lessonId || this.readOnly()) return;
      this.error = "";
      this.revision.busy = true;
      const before = { deck: this.deck, cards: this.cards.map((c) => ({ ...c, _state: undefined })) };
      try {
        const res = await (await api(`/api/lessons/${this.lessonId}/revise`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...this.payload(), instruction }),
        })).json();
        const { cards, stats } = this.compareCards(before.cards, res.lesson.cards);
        clearTimeout(this.saveTimer);
        this.saveTimer = null;
        this.deck = res.lesson.deck;
        this.cards = cards;
        this.lastSaved = this.snapshot();  // the server already saved the revision
        this.saveState = "saved";
        this.revision = { text: "", busy: false, summary: res.summary, stats, undo: before };
        this.loadLessons();
        this.drawPictures();  // a card added by the correction may ask for one
      } catch (e) {
        this.error = e.message;
        this.revision.busy = false;
      }
    },

    // Keep the keys of unchanged cards and flag new or modified ones.
    compareCards(oldCards, newCards) {
      const same = (a, b) => ["front", "back", "info", "subdeck"].every((f) => (a[f] ?? "") === (b[f] ?? ""));
      const unused = [...oldCards];
      let added = 0, modified = 0;
      const cards = newCards.map((card) => {
        const exact = unused.findIndex((o) => same(o, card));
        if (exact !== -1) return { ...card, key: unused.splice(exact, 1)[0].key };
        const sameFront = unused.findIndex((o) => o.front === card.front);
        if (sameFront !== -1) {
          modified++;
          return { ...withKey(card), key: unused.splice(sameFront, 1)[0].key, _state: "modified" };
        }
        added++;
        return { ...withKey(card), _state: "new" };
      });
      const parts = [];
      if (added) parts.push(t("app.revise.added", { count: added }));
      if (modified) parts.push(t("app.revise.modified", { count: modified }));
      if (unused.length) parts.push(t("app.revise.removed", { count: unused.length }));
      return { cards, stats: parts.join(" · ") || t("app.revise.noChange") };
    },

    // The lesson as it was, for "Undo" after a regeneration (a correction only needs
    // the deck and cards)
    undoState() {
      return {
        deck: this.deck,
        cards: this.cards.map((c) => ({ ...c, _state: undefined })),
        frames: this.frames,
        lessonPrompt: this.lessonPrompt,
        lessonChoice: this.lessonChoice,
        voice: this.form.voice,
        reverse: this.reverse,
        typing: this.typing,
        dictation: this.dictation,
      };
    },

    undoRevision() {
      const { deck, cards, voice, ...rest } = this.revision.undo;
      this.deck = deck;
      this.cards = cards;  // autosave sends the restored version
      if (voice !== undefined) this.form.voice = voice;
      Object.assign(this, rest);  // after a regeneration: frames, prompt, options
      this.revision = { text: "", busy: false, summary: t("app.revise.undone"), stats: "", undo: null };
    },

    clearRevision() {
      this.cards.forEach((c) => { c._state = undefined; });
      this.revision = { ...this.revision, summary: "", stats: "", undo: null };
    },

    // --- Review ---------------------------------------------------------
    // Photos + prompt, or the prompt alone (see "Generate from the prompt alone")
    canGenerate() {
      return this.form.text.trim() !== "" && !this.loading;
    },

    bottomBar() {
      return true;  // always visible: it is the main call to action
    },

    removeCard(card) {
      this.cards = this.cards.filter((c) => c !== card);
    },

    // --- Diagram labels (masks on a photo) ---------------------------------
    photoFor(card) {
      return this.photos[card.mask.page - 1]?.url ?? null;
    },

    hasMasks() {
      return this.cards.some((c) => c.mask);
    },

    // Every mask on the same photo: all of them are hidden on the card's image.
    masksOnPage(card) {
      return this.cards.filter((c) => c.mask && c.mask.page === card.mask.page);
    },

    // What Anki shows of the photo: the diagram's frame stretched to hold every mask
    // with a margin, so it never cuts a label (same as diagrams.crop on the server).
    // No frame: the whole photo.
    cropFor(card) {
      const frame = this.frames.find((f) => f.page === card.mask.page);
      if (!frame) return { box: [0, 0, 1, 1] };
      const m = 0.03;
      const masks = this.masksOnPage(card).map(({ mask: { box: b } }) => [b[0] - m, b[1] - m, b[2] + m, b[3] + m]);
      const boxes = [frame.box, ...masks];
      const clamp = (v) => Math.min(1, Math.max(0, v));
      return {
        box: [
          clamp(Math.min(...boxes.map((b) => b[0]))), clamp(Math.min(...boxes.map((b) => b[1]))),
          clamp(Math.max(...boxes.map((b) => b[2]))), clamp(Math.max(...boxes.map((b) => b[3]))),
        ],
      };
    },

    // Drag a corner of the frame ("nw", "ne", "sw", "se"). It starts from what is shown,
    // and can't go inside the masks: the crop stretches back to hold them.
    startCrop(event, card, corner) {
      if (this.readOnly()) return;
      event.preventDefault();
      const rect = event.currentTarget.closest(".diagram").getBoundingClientRect();
      let frame = this.frames.find((f) => f.page === card.mask.page);
      if (!frame) {
        frame = { page: card.mask.page, box: [0, 0, 1, 1] };
        this.frames.push(frame);
      }
      frame.box = [...this.cropFor(card).box];
      const start = { x: event.clientX, y: event.clientY, box: [...frame.box] };
      const clamp = (v, min, max) => Math.min(max, Math.max(min, v));
      const move = (e) => {
        const dx = (e.clientX - start.x) / rect.width;
        const dy = (e.clientY - start.y) / rect.height;
        let [x0, y0, x1, y1] = start.box;
        if (corner.includes("w")) x0 = clamp(x0 + dx, 0, x1 - 0.05);
        if (corner.includes("e")) x1 = clamp(x1 + dx, x0 + 0.05, 1);
        if (corner.includes("n")) y0 = clamp(y0 + dy, 0, y1 - 0.05);
        if (corner.includes("s")) y1 = clamp(y1 + dy, y0 + 0.05, 1);
        frame.box = [x0, y0, x1, y1].map((v) => Math.round(v * 10000) / 10000);
      };
      const stop = () => {
        frame.box = [...this.cropFor(card).box];  // what is shown: pushed inside a mask, it stops there
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", stop);
        window.removeEventListener("pointercancel", stop);
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", stop);
      window.addEventListener("pointercancel", stop);
    },

    maskStyle(mask) {
      const [x0, y0, x1, y1] = mask.box.map((v) => v * 100);
      return `left: ${x0}%; top: ${y0}%; width: ${x1 - x0}%; height: ${y1 - y0}%`;
    },

    // Drag the mask (mode "move") or its corner ("resize"), with a finger or a mouse.
    // Boxes are fractions of the photo, like on the server.
    startDrag(event, card, mode) {
      if (this.readOnly()) return;
      event.preventDefault();
      const frame = event.currentTarget.closest(".diagram").getBoundingClientRect();
      const start = { x: event.clientX, y: event.clientY, box: [...card.mask.box] };
      const clamp = (v, min, max) => Math.min(max, Math.max(min, v));
      const move = (e) => {
        const dx = (e.clientX - start.x) / frame.width;
        const dy = (e.clientY - start.y) / frame.height;
        let [x0, y0, x1, y1] = start.box;
        if (mode === "move") {
          const [w, h] = [x1 - x0, y1 - y0];
          x0 = clamp(x0 + dx, 0, 1 - w);
          y0 = clamp(y0 + dy, 0, 1 - h);
          [x1, y1] = [x0 + w, y0 + h];
        } else {
          x1 = clamp(x1 + dx, x0 + 0.02, 1);
          y1 = clamp(y1 + dy, y0 + 0.02, 1);
        }
        card.mask.box = [x0, y0, x1, y1].map((v) => Math.round(v * 10000) / 10000);
      };
      const stop = () => {
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", stop);
        window.removeEventListener("pointercancel", stop);
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", stop);
      window.addEventListener("pointercancel", stop);
    },

    subdecks() {
      return [...new Set(this.cards.map((c) => c.subdeck).filter(Boolean))];
    },

    addCard() {
      const last = this.cards.at(-1);
      this.cards.push(withKey({ id: newId(), front: "", back: "", subdeck: last?.subdeck ?? "" }));
    },

    // --- Formulas --------------------------------------------------------
    hasMath(text) {
      return HAS_MATH.test(text ?? "");
    },

    // A front grows with its text (a sentence with gaps is long). CSS does it where
    // `field-sizing` is known; elsewhere, its height follows the text it holds, and
    // is measured again when it shows or its width changes.
    watchHeight(el) {
      if (!CSS.supports("field-sizing", "content")) new ResizeObserver(() => this.fitHeight(el)).observe(el);
    },
    fitHeight(el) {
      if (CSS.supports("field-sizing", "content") || !el.offsetParent) return; // hidden: no height to measure
      el.style.height = "auto";
      el.style.height = `${el.scrollHeight}px`;
    },

    isCloze(text) {
      return CLOZE.test(text ?? "");
    },

    // The text as Anki will show it: formulas drawn (KaTeX), gaps marked with their
    // number (each number is a card), line breaks kept, the rest escaped.
    previewHtml(text) {
      text = text ?? "";
      const plain = (t) =>
        escapeHtml(t)
          .replace(GAP_START, (_, n) => `<span class="gap"><sup>${n}</sup>`)
          .replace(GAP_END, "</span>")
          .replaceAll("\n", "<br>");
      if (!this.katexReady) return plain(text);
      let html = "", last = 0;
      for (const m of text.matchAll(MATH)) {
        html += plain(text.slice(last, m.index));
        const display = m[2] !== undefined;
        html += katex.renderToString(m[1] ?? m[2], { displayMode: display, throwOnError: false });
        last = m.index + m[0].length;
      }
      return html + plain(text.slice(last));
    },

    // --- Pictures ("front: the picture of the word") ------------------------
    pictureUrl(card) {
      return `/api/lessons/${this.lessonId}/pictures/${card.picture}`;
    },

    // A card's picture panel: what to draw (the AI's subject, else the answer), redraw,
    // the user's own photo, or no picture.
    togglePicturePanel(card) {
      card._panel = !card._panel;
      if (card._panel) card._subject = card.picture_prompt || card.back;
    },

    async pictureAction(card, request) {
      if (this.saveTimer) await this.saveNow();  // the server must know the card
      card._drawing = true;
      this.pictureJobs++;
      this.error = "";
      try {
        const res = await (await api(`/api/lessons/${this.lessonId}/cards/${card.id}/picture${request.path ?? ""}`, request)).json();
        card.picture = res.card.picture;
        card.picture_prompt = res.card.picture_prompt;
        if (!card.picture) card._panel = false;
      } catch (e) {
        this.error = e.message;
      } finally {
        card._drawing = false;
        this.pictureJobs--;
      }
    },

    redrawPicture(card) {
      return this.pictureAction(card, {
        path: "/draw",
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ subject: card._subject ?? null }),
      });
    },

    async uploadPicture(card, event) {
      const file = event.target.files[0];
      event.target.value = "";
      if (!file) return;
      const body = new FormData();
      body.append("photo", await resize(file), "photo.jpg");
      return this.pictureAction(card, { method: "POST", body });
    },

    removePicture(card) {
      return this.pictureAction(card, { method: "DELETE" });
    },

    async drawPictures() {
      if (this.readOnly() || !this.cards.some((c) => c.picture_prompt && !c.picture)) return;
      if (this.saveTimer) await this.saveNow();
      this.drawing = true;
      const lessonId = this.lessonId;
      try {
        const res = await (await api(`/api/lessons/${lessonId}/pictures`, { method: "POST" })).json();
        if (this.lessonId !== lessonId) return;  // another lesson opened meanwhile
        const drawn = new Map(res.lesson.cards.map((c) => [c.id, c.picture]));
        this.cards.forEach((c) => { if (!c.picture && drawn.get(c.id)) c.picture = drawn.get(c.id); });
        if (res.failures) {
          this.error = t("app.pictures.failed", { count: res.failures }) + (res.error ? ` ${errorMessage(res.error)}` : "");
        }
      } catch (e) {
        this.error = e.message;
      } finally {
        this.drawing = false;
      }
    },

    // --- Audio ----------------------------------------------------------
    // edge-tts voice ("es-ES-ElviraNeural") → mp3 in the package;
    // Anki locale ("es_ES") → the device reads it aloud.
    isVoice(voice) {
      return /^[a-z]{2,3}-[A-Z]{2}-\w+$/.test(voice);
    },

    hasAudio() {
      return this.isVoice(this.form.voice);
    },

    sampleText(voice) {
      const samples = { es: "Hola, ¿cómo estás?", en: "Hello, how are you?", de: "Hallo, wie geht's?",
                        it: "Ciao, come stai?", fr: "Bonjour, comment ça va ?", pt: "Olá, tudo bem?" };
      return samples[voice.slice(0, 2)] ?? "Hello!";
    },

    play(text, voice = this.form.voice) {
      if (!text.trim()) return;
      const params = new URLSearchParams({ text, voice });
      if (this.lessonId) params.set("lesson", this.lessonId);  // kept in the lesson, reused on export
      const audio = new Audio(`/api/tts?${params}`);
      // Only a load failure means the sound is really missing. play() may also
      // reject when playback start is interrupted (e.g. AbortError) while the
      // sound still plays, so its rejection alone is not an error for the user.
      audio.addEventListener("error", () => {
        this.error = t("app.audio.failed");
      });
      audio.play().catch((e) => {
        if (e.name === "NotAllowedError") this.error = t("app.audio.blocked");
        else console.warn("audio.play()", e);
      });
    },

    // --- Direct send (AnkiConnect) -------------------------------------
    async checkAnki() {
      const before = this.anki.profile;
      try {
        this.anki = await (await api("/api/anki/status")).json();
      } catch {
        this.anki = { available: false };
      }
      const now = this.anki.profile;
      // Another Anki profile opened (not at page load, not on the profile screen)
      if (before && now && now !== before) this.profileToApply = now;
      if (this.profileToApply) await this.applyProfile();
    },

    // Follow an Anki profile switch: a lesson of the previous profile is saved and
    // closed, so nothing gets sent to the wrong collection. Waits for a generation,
    // correction or send in progress to finish, so their result isn't lost.
    async applyProfile() {
      if (this.loading || this.revision.busy || this.sending || this.exporting) return;
      const profile = this.profileToApply;
      this.profileToApply = null;
      this.loadLessons();  // the new profile's lessons
      if (this.lessonId && this.lessonOwner && !this.lessonShared && this.lessonOwner !== profile) {
        if (this.saveTimer) await this.saveNow();
        await this.newLesson();
      }
      this.success = t("app.profile.switched", { profile });
      setTimeout(() => { if (this.success.includes(profile)) this.success = ""; }, 5000);
    },

    async sendToAnki() {
      this.error = this.success = "";
      this.sending = true;
      try {
        const r = await (await api("/api/anki/send", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...this.payload(), lesson_id: this.lessonId }),
        })).json();
        this.lastSaved = this.snapshot();  // sending also saves the lesson
        this.lessonExported = true;
        clearTimeout(this.saveTimer);
        this.saveTimer = null;
        this.saveState = "saved";
        this.loadLessons();
        const parts = [];
        if (r.added) parts.push(t("app.send.added", { count: r.added }));
        if (r.updated) parts.push(t("app.send.updated", { count: r.updated }));
        let message = t("app.send.done", { parts: parts.join(", ") || t("app.send.nothing") });
        if (r.synced) message += " " + t("app.send.synced");
        this.success = message;
        const warnings = [];
        if (r.sync_error) warnings.push(t("app.send.noSync", { reason: errorMessage(r.sync_error) }));
        if (r.audio_failures) warnings.push(t("app.send.noSound", { count: r.audio_failures }));
        if (warnings.length) this.error = t("app.send.butWarning", { warnings: warnings.join(" ; ") });
        setTimeout(() => { if (this.success === message) this.success = ""; }, 6000);
      } catch (e) {
        this.error = e.message;
        this.checkAnki();
      } finally {
        this.sending = false;
      }
    },

    // --- Export ---------------------------------------------------------
    async exportApkg() {
      this.error = "";
      this.exporting = true;
      try {
        const res = await api("/api/export", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...this.payload(), lesson_id: this.lessonId }),
        });
        this.lastSaved = this.snapshot();  // exporting also saves the lesson
        this.lessonExported = true;
        clearTimeout(this.saveTimer);
        this.saveTimer = null;
        this.saveState = "saved";
        this.loadLessons();
        const url = URL.createObjectURL(await res.blob());
        const a = document.createElement("a");
        a.href = url;
        a.download = `${this.deck.replace(/[\\/:*?"<>|]+/g, " - ")}.apkg`;
        a.click();
        setTimeout(() => URL.revokeObjectURL(url), 10_000);
        const failures = Number(res.headers.get("X-Cartable-Audio-Failures") || 0);
        if (failures) this.error = t("app.export.noSound", { count: failures });
      } catch (e) {
        this.error = e.message;
      } finally {
        this.exporting = false;
      }
    },
  }));
});
