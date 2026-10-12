/* Separate audited desktop integration; initialized with the existing Cl executor. */
(() => {
  "use strict";
  const BASE = "http://127.0.0.1:18766";
  const ALARM = "trancy-desktop-heartbeat-v1";
  const videos = new Map();
  const reports = new Map();
  const tabSequences = new Map();
  const running = new Set();
  const captionRequests = new Map();
  const clientId = crypto.randomUUID();
  let session = "", timer = null, ticking = false, lastKeepalive = 0, api = null;

  function pageIdentity(raw) {
    try {
      const url = new URL(raw);
      if (url.protocol !== "https:" || url.username || url.password || (url.port && url.port !== "443")) return null;
      if (["youtube.com", "www.youtube.com", "m.youtube.com", "www.youtube-nocookie.com"].includes(url.hostname)) {
        const match = url.pathname.match(/^\/(?:embed|live|shorts)\/([A-Za-z0-9_-]{11})\/?$/);
        const id = url.pathname === "/watch" ? url.searchParams.get("v") : match?.[1];
        return /^[A-Za-z0-9_-]{11}$/.test(id || "") ? `youtube:${id}` : null;
      }
      if (url.hostname === "bilibili.com" || url.hostname.endsWith(".bilibili.com")) {
        const match = url.pathname.match(/^\/video\/(BV[A-Za-z0-9]{10})\/?$/);
        const page = Number(url.searchParams.get("p") || 1);
        return match && Number.isInteger(page) && page > 0 && page <= 10000 ? `bilibili:${match[1]}:${page}` : null;
      }
    } catch (_) { /* Unsupported URL. */ }
    return null;
  }

  function mediaAllowed(raw, platform, cid) {
    try {
      const url = new URL(raw);
      if (url.protocol !== "https:" || url.username || url.password || (url.port && url.port !== "443")) return false;
      const domains = platform === "youtube" ? ["googlevideo.com"] : ["bilibili.com", "bilivideo.com", "bilivideo.cn", "hdslb.com"];
      return domains.some(d => url.hostname === d || url.hostname.endsWith(`.${d}`)) &&
        (platform === "youtube" || (/^[1-9][0-9]{0,19}$/.test(cid) && new RegExp(`(^|[^0-9])${cid}([^0-9]|$)`).test(url.pathname)));
    } catch (_) { return false; }
  }

  function sanitizeVideo(raw, sender) {
    const identity = pageIdentity(sender.tab.url);
    if (!raw || pageIdentity(raw.url) !== identity) return null;
    const expected = raw.platform === "youtube" ? `youtube:${raw.video_id}` : `bilibili:${raw.video_id}:${raw.page}`;
    if (identity !== expected) return null;
    const cid = /^[1-9][0-9]{0,19}$/.test(String(raw.cid || "")) ? String(raw.cid) : "";
    const urls = Array.isArray(raw.audio_urls) ? raw.audio_urls : [];
    return {platform: raw.platform, video_id: raw.video_id, url: raw.url, page: Number(raw.page) || 1,
      cid, title: String(raw.title || "").slice(0, 600), position: Number(raw.position) || 0,
      duration: Number(raw.duration) || 0, rate: Number(raw.rate) || 1, paused: Boolean(raw.paused),
      active: Boolean(sender.tab.active && raw.active), ad_playing: Boolean(raw.ad_playing),
      is_live: Boolean(raw.is_live),
      document_start: Number(raw.document_start) || 0,
      audio_urls: urls.filter(u => typeof u === "string" && u.length <= 16384 && mediaAllowed(u, raw.platform, cid)).slice(0, 12)};
  }

  async function post(path, payload, useSession = true) {
    const response = await fetch(BASE + path, {method: "POST", cache: "no-store", credentials: "omit",
      headers: {"Content-Type": "application/json", "X-Trancy-Desktop": "1", "X-Trancy-Extension": chrome.runtime.id,
        ...(useSession ? {"X-Trancy-Session": session} : {})},
      body: JSON.stringify({client_id: clientId, ...payload}), signal: AbortSignal.timeout(2500)});
    if (!response.ok) throw new Error(`desktop ${response.status}`);
    return response.json();
  }

  async function translateJob(job) {
    if (!job || typeof job.id !== "string" || running.has(job.id) || !Array.isArray(job.texts) ||
        job.texts.length > 30 || job.texts.some(t => typeof t !== "string" || t.length > 30000) ||
        job.texts.reduce((n, t) => n + t.length, 0) > 30000 ||
        !["en", "zh", "zh-CN", "zh-TW", "auto"].includes(job.source) || !["en", "zh-CN", "zh-TW"].includes(job.target)) return;
    running.add(job.id);
    try {
      const state = await api.getState();
      const engine = state?.translatorService?.subtitle;
      if (!engine) throw new Error("missing subtitle engine");
      // Keys/tokens are read only here and never included in a localhost payload.
      const result = await api.translate({texts: job.texts, from: job.source, to: job.target, engine,
        useCache: true, ut: state.user?.token});
      if (!Array.isArray(result) || result.length !== job.texts.length || result.some(t => t?.message !== "ok" || typeof t.translation !== "string")) {
        throw new Error("translation failed");
      }
      await post("/v1/result", {id: job.id, translations: result.map(t => t.translation)});
    } catch (_) {
      // Provider errors can contain credentials; only a fixed error crosses the bridge.
      await post("/v1/result", {id: job.id, error: "plugin_translation_failed"}).catch(() => {});
    } finally { running.delete(job.id); }
  }

  const nextSequence = tabId => { const n = (tabSequences.get(tabId) || 0) + 1; tabSequences.set(tabId, n); return n; };

  function sameCaptionVideo(left, right) {
    return left && right && pageIdentity(left.url) === pageIdentity(right.url) &&
      left.platform === right.platform && left.video_id === right.video_id &&
      Number(left.page) === Number(right.page) && String(left.cid || "") === String(right.cid || "");
  }

  async function captionJob(job) {
    if (!job || !/^[a-f0-9]{32}$/.test(job.id || "") || !Number.isSafeInteger(job.tab_id) ||
        captionRequests.has(job.id)) return;
    const entry = videos.get(job.tab_id);
    if (!entry || !sameCaptionVideo(entry.video, job.video) || entry.documentStart !== job.document_start) {
      await post("/v1/captions", {id: job.id, tab_id: job.tab_id, video: job.video,
        document_start: job.document_start, status: "error"}).catch(() => {});
      return;
    }
    captionRequests.set(job.id, {...job, started: Date.now()});
    try {
      const reply = await chrome.tabs.sendMessage(job.tab_id, {to: [], kind: "trancy-desktop-captions-request-v1",
        request_id: job.id, document_start: job.document_start, video: job.video,
        preferred_language: job.preferred_language}, {frameId: 0});
      if (!reply?.accepted) throw new Error("caption observer unavailable");
    } catch (_) {
      captionRequests.delete(job.id);
      await post("/v1/captions", {id: job.id, tab_id: job.tab_id, video: job.video,
        document_start: job.document_start, status: "error"}).catch(() => {});
    }
  }

  async function captionResult(message, sender) {
    const job = captionRequests.get(message.request_id);
    const entry = videos.get(sender.tab.id);
    if (!job || job.tab_id !== sender.tab.id || message.document_start !== job.document_start ||
        !entry || entry.documentStart !== job.document_start || !sameCaptionVideo(entry.video, job.video) ||
        !sameCaptionVideo(message.video, job.video)) return;
    captionRequests.delete(job.id);
    const base = {id: job.id, tab_id: job.tab_id, video: job.video, document_start: job.document_start};
    if (message.status === "unavailable" || message.status === "error") {
      await post("/v1/captions", {...base, status: message.status}).catch(() => {});
      return;
    }
    if (message.status !== "ready" || !Array.isArray(message.cues) || !message.cues.length || message.cues.length > 20000) {
      await post("/v1/captions", {...base, status: "error"}).catch(() => {});
      return;
    }
    // Only public subtitle text/times are forwarded. Even a spoofed page event
    // cannot smuggle its fetch headers, signed URLs or unrelated JSON fields.
    const chunks = []; let chunk = [], size = 0;
    try {
      for (const raw of message.cues) {
        if (!raw || typeof raw.text !== "string" || !raw.text.trim() || raw.text.length > 16000 ||
            !Number.isFinite(raw.start) || !Number.isFinite(raw.end) || raw.start < 0 || raw.end <= raw.start || raw.end > 604800) {
          throw new Error("invalid cue");
        }
        const cue = {start: raw.start, end: raw.end, text: raw.text,
          source_language: String(raw.source_language || message.source_language || "").slice(0, 32),
          subtitle_kind: raw.subtitle_kind === "asr" ? "asr" : "human"};
        if (typeof raw.translation === "string" && raw.translation.trim() && raw.translation.length <= 16000) {
          cue.translation = raw.translation; cue.target_language = String(raw.target_language || "").slice(0, 32);
        }
        if (Array.isArray(raw.words) && raw.words.length <= 1000) {
          cue.words = raw.words.filter(w => w && typeof w.text === "string" && w.text.trim() && w.text.length <= 500 &&
            Number.isFinite(w.start) && Number.isFinite(w.end) && raw.start <= w.start && w.start < w.end && w.end <= raw.end)
            .map(w => ({text: w.text, start: w.start, end: w.end}));
        }
        const length = JSON.stringify(cue).length;
        if (length > 70000) throw new Error("large cue");
        if (chunk.length && (chunk.length >= 160 || size + length > 80000)) {chunks.push(chunk); chunk = []; size = 0;}
        chunk.push(cue); size += length;
      }
      if (chunk.length) chunks.push(chunk);
      if (chunks.length > 256) throw new Error("large track");
      for (let i = 0; i < chunks.length; i++) {
        const current = videos.get(job.tab_id);
        if (!current || current.documentStart !== job.document_start || !sameCaptionVideo(current.video, job.video)) return;
        const reply = await post("/v1/captions", {...base, status: "ready", part: i, part_count: chunks.length, cues: chunks[i],
          source_language: String(message.source_language || "").slice(0, 32),
          caption_source: ["youtube-human", "youtube-asr", "bilibili-human", "bilibili-asr"].includes(message.caption_source) ? message.caption_source : ""});
        if (reply.accepted === false) return; // Stopped/replaced request: no remaining text uploads.
      }
    } catch (_) {
      await post("/v1/captions", {...base, status: "error"}).catch(() => {});
    }
  }

  async function closeTab(tabId) {
    videos.delete(tabId);
    for (const [id, job] of captionRequests) if (job.tab_id === tabId) captionRequests.delete(id);
    if (session) await post("/v1/closed", {tab_id: tabId, seq: nextSequence(tabId)}).catch(() => {});
  }

  function schedule(delay) {
    clearTimeout(timer);
    timer = setTimeout(tick, delay);
  }

  async function tick() {
    if (ticking || !api) return;
    ticking = true;
    let connected = false;
    try {
      if (!session) session = (await post("/v1/hello", {}, false)).session;
      const reply = await post("/v1/poll", {});
      connected = true;
      for (const job of reply.jobs || []) void translateJob(job);
      for (const job of reply.caption_jobs || []) void captionJob(job);
      const now = Date.now();
      for (const [id, job] of captionRequests) if (now - job.started > 15000) captionRequests.delete(id);
      for (const [tabId, entry] of videos) {
        if (now - entry.seen > 5000) { await closeTab(tabId); continue; }
        if (!entry.dirty) continue;
        entry.dirty = false;
        await post("/v1/video", {tab_id: tabId, seq: nextSequence(tabId), video: entry.video});
      }
      if (now - lastKeepalive > 20000) {
        lastKeepalive = now;
        // Only while a desktop app is connected. A Chrome API event keeps this
        // MV3 worker available for system-audio translations without a video tab.
        await chrome.runtime.getPlatformInfo();
      }
    } catch (_) {
      session = "";
      for (const entry of videos.values()) entry.dirty = true;
    } finally {
      ticking = false;
      schedule(connected ? 400 : 5000);
    }
  }

  self.TrancyDesktopBridge = {
    start(dependencies) {
      if (api) return;
      // The connector has its own identity. The original Trancy installation
      // must not compete for desktop jobs or log connection failures.
      if (chrome.runtime.getManifest().name !== "Trancy 桌面字幕连接器") return;
      api = dependencies;
      chrome.runtime.onMessage.addListener((message, sender) => {
        if (!["trancy-desktop-report-v1", "trancy-desktop-captions-report-v1"].includes(message?.kind) || sender.id !== chrome.runtime.id ||
            sender.frameId !== 0 || !sender.tab || !pageIdentity(sender.tab.url) ||
            pageIdentity(message.href) !== pageIdentity(sender.tab.url)) return;
        if (message.kind === "trancy-desktop-captions-report-v1") { void captionResult(message, sender); return; }
        const previous = reports.get(sender.tab.id);
        const docStart = Number(message.document_start), sequence = Number(message.sequence);
        if (!Number.isFinite(docStart) || !Number.isSafeInteger(sequence) ||
            (previous && (docStart < previous.documentStart || (docStart === previous.documentStart && sequence <= previous.sequence)))) return;
        reports.set(sender.tab.id, {documentStart: docStart, sequence});
        if (message.video === null) { void closeTab(sender.tab.id); return; }
        const video = sanitizeVideo({...message.video, document_start: docStart}, sender);
        if (!video) return;
        videos.set(sender.tab.id, {video, documentStart: docStart, sequence, seen: Date.now(), dirty: true});
      });
      chrome.tabs.onRemoved.addListener(tabId => { reports.delete(tabId); void closeTab(tabId); });
      chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
        const entry = videos.get(tabId);
        if (entry && changeInfo.url && pageIdentity(changeInfo.url) !== pageIdentity(entry.video.url)) void closeTab(tabId);
      });
      chrome.alarms.onAlarm.addListener(alarm => { if (alarm.name === ALARM) void tick(); });
      chrome.alarms.create(ALARM, {periodInMinutes: .5});
      void tick();
    }
  };
})();
