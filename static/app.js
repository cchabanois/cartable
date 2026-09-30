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

let nextKey = 0;
const withKey = (card) => ({ info: "", subdeck: "", tags: [], ...card, key: nextKey++ });

document.addEventListener("alpine:init", () => {
  Alpine.data("cartable", () => ({
    photos: [],          // { blob, url }
    prompts: [],
    selectedId: null,
    form: { name: "", text: "", deck: "", voice: "" },
    voices: [],          // edge-tts voices: { voice, locale, gender }
    deck: "",
    cards: [],
    reverse: false,
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
    editor: { open: false, id: null, name: "", text: "", deck: "", voice: "", error: "" },
    error: "",
    success: "",
    anki: { available: false },  // Anki reachable → direct send; `profile`: open Anki profile
    settingsHere: true,          // false on a phone when Cartable runs in the Anki add-on
    profileToApply: null,        // Anki profile switch waiting for the current task to finish
    allProfiles: false,          // "My lessons": show every Anki profile's lessons
    allProfilesAllowed: true,    // that switch can be disabled in the settings
    lessonOwner: "",              // Anki profile that created the open lesson ("" = nobody: shared)
    lessonShared: false,          // visible from every profile (only the owner's profile can change it)
    sending: false,

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

      await i18nReady;  // the language is needed for the first default prompts
      try {
        this.settingsHere = (await (await fetch("/api/admin")).json()).allowed;
        this.allProfilesAllowed = (await (await api("/api/config")).json()).all_profiles_view;
      } catch {}
      await Promise.all([this.loadPrompts(Number(storage("get")) || null), this.loadLessons()]);
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
        } catch {
          this.error = t("app.photos.unreadable", { name: file.name });
        }
      }
      event.target.value = "";  // allows picking the same photo again
    },

    removePhoto(i) {
      URL.revokeObjectURL(this.photos[i].url);
      this.photos.splice(i, 1);
    },

    // --- Prompts ---------------------------------------------------------
    async loadPrompts(selectId) {
      try {
        this.prompts = await (await api("/api/prompts")).json();
      } catch (e) {
        this.error = t("app.prompt.unavailable", { message: e.message });
        return;
      }
      const found = this.prompts.find((c) => c.id === selectId) ?? this.prompts[0];
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
        ? { name: c.name, text: c.text, deck: c.deck, voice: c.voice }
        : { name: "", text: "", deck: "", voice: "" };
      if (c) storage("set", c.id);
    },

    isModified() {
      const c = this.current();
      return c && c.text !== this.form.text;
    },

    // Editor sheet: "new" = empty form, "edit" = the selected prompt,
    // "copy" = a new prompt starting from the text tweaked for this time.
    openEditor(mode) {
      const c = this.current();
      const base = mode === "new" ? { name: "", text: "", deck: "", voice: c?.voice ?? "" } : { ...this.form };
      if (mode === "copy") base.name = "";
      this.editor = { open: true, id: mode === "edit" ? c.id : null, error: "", ...base };
      this.$nextTick(() => {
        if (!this.editor.name) this.$refs.editorName.focus();
      });
    },

    async saveEditor() {
      const { id, name, text, deck, voice } = this.editor;
      if (!name.trim() || !text.trim()) {
        this.editor.error = t("app.editor.required");
        return;
      }
      try {
        const saved = await (await api(id ? `/api/prompts/${id}` : "/api/prompts", {
          method: id ? "PUT" : "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: name.trim(), text: text.trim(), deck: deck.trim(), voice: voice.trim() }),
        })).json();
        this.editor.open = false;
        await this.loadPrompts(saved.id);
      } catch (e) {
        this.editor.error = t("common.failed", { message: e.message });
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
    async extract() {
      this.error = "";
      this.loading = true;
      const body = new FormData();
      this.photos.forEach((p, i) => body.append("images", p.blob, `page-${i + 1}.jpg`));
      body.append("prompt", this.form.text);
      body.append("deck", this.form.deck);
      body.append("voice", this.form.voice);
      if (this.selectedId) body.append("prompt_id", this.selectedId);
      try {
        const lesson = await (await api("/api/extract", { method: "POST", body })).json();
        const used = this.current();
        if (used) used.used_at = new Date().toISOString();  // moves it to the front of the chips
        this.show(lesson);
        this.loadLessons();
        if (!this.cards.length) this.error = t("app.review.noCards");
      } catch (e) {
        this.error = e.message;
      } finally {
        this.loading = false;
      }
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

    // Lessons of the open Anki profile, shared lessons, lessons without owner —
    // or all of them with the "All profiles" switch (when the settings allow it).
    visibleLessons() {
      const profile = this.anki.profile;
      if ((this.allProfiles && this.allProfilesAllowed) || !profile) return this.lessons;
      return this.lessons.filter((l) => l.shared || !l.owner || l.owner === profile);
    },

    // Other profiles have private lessons (the "All profiles" switch is useful)
    otherProfiles() {
      return this.lessons.some((l) => l.owner && !l.shared && l.owner !== this.anki.profile);
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
      this.deck = lesson.deck;
      this.cards = lesson.cards.map(withKey);
      this.reverse = lesson.reverse;
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
        this.clearPhotos();
        // Photos come back as blobs, so a generation can be run again.
        for (let n = 1; n <= lesson.photo_count; n++) {
          const blob = await (await api(`/api/lessons/${id}/photos/${n}`)).blob();
          this.photos.push({ blob, url: URL.createObjectURL(blob) });
        }
        this.show(lesson);
      } catch (e) {
        this.error = t("app.lessons.openFailed", { message: e.message });
      }
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
      this.saveState = "";
      this.selectPrompt();  // restores the selected prompt's voice
      window.scrollTo({ top: 0, behavior: "smooth" });
    },

    async deleteLesson(l) {
      if (!confirm(t("app.lessons.confirmDelete", { deck: l.deck }))) return;
      try {
        await api(`/api/lessons/${l.id}`, { method: "DELETE" });
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
        cards: this.cards.map(({ key, _state, ...card }) => card),
        voice: this.form.voice,
        reverse: this.reverse,
        shared: this.lessonShared,
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

    undoRevision() {
      const { deck, cards } = this.revision.undo;
      this.deck = deck;
      this.cards = cards;  // autosave sends the restored version
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

    subdecks() {
      return [...new Set(this.cards.map((c) => c.subdeck).filter(Boolean))];
    },

    addCard() {
      const last = this.cards.at(-1);
      this.cards.push(withKey({ front: "", back: "", subdeck: last?.subdeck ?? "" }));
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
      this.allProfiles = false;
      if (this.lessonId && this.lessonOwner && !this.lessonShared && this.lessonOwner !== profile) {
        if (this.saveTimer) await this.saveNow();
        await this.newLesson();
      }
      this.success = t("app.profile.switched", { profile });
      setTimeout(() => { if (this.success.includes(profile)) this.success = ""; }, 5000);
    },

    async sendToAnki(force = false) {
      this.error = this.success = "";
      this.sending = true;
      try {
        const r = await (await api("/api/anki/send", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...this.payload(), lesson_id: this.lessonId, force }),
        })).json();
        this.lastSaved = this.snapshot();  // sending also saves the lesson
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
        this.sending = false;
        if (e.status === 409) {
          // Lesson made for another Anki profile than the open one
          const ok = confirm(t("app.send.confirmOtherProfile", e.detail.params));
          if (ok) return this.sendToAnki(true);
          return;
        }
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
