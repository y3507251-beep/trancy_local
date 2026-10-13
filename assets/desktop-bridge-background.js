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
  const dubbingRequests = new Map();
  const dubbingLeases = new Map();
  const dubbingStatuses = new Set(["active", "restored", "user_override", "expired", "video_changed", "error"]);
  const dubbingReasons = new Set(["", "disabled", "bridge_disconnected", "lease_expired", "video_changed", "ad_playing", "retired_lease", "replaced", "user_volume", "page_closed", "observer_unavailable"]);
  const captionErrorCodes = new Set(["bili_metadata_failed", "bili_track_failed", "youtube_metadata_failed", "youtube_track_failed",
    "observer_unavailable", "video_changed", "invalid_caption_payload", "bridge_upload_failed"]);
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
      ended: raw.ended === true,
      active: Boolean(sender.tab.active && raw.active), ad_playing: Boolean(raw.ad_playing),
      is_live: Boolean(raw.is_live),
      seeking: Boolean(raw.seeking), buffering: Boolean(raw.buffering),
      seek_serial: Number.isSafeInteger(raw.seek_serial) && raw.seek_serial >= 0 ? raw.seek_serial : 0,
      dubbing_protocol: raw.dubbing_protocol === 1 ? 1 : 0,
      playback_event: ["play", "pause", "ended", "seeking", "seeked", "waiting", "stalled", "playing", "ratechange", "loadedmetadata", "emptied"].includes(raw.playback_event) ? raw.playback_event : "",
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

  async function dubbingJob(job) {
    if (!job || !/^[a-f0-9]{32}$/.test(job.id || "") || !/^[a-f0-9]{32}$/.test(job.lease_id || "") ||
        typeof job.enabled !== "boolean" || !Number.isSafeInteger(job.tab_id) || job.video?.platform !== "bilibili" ||
        dubbingRequests.has(job.id)) return;
    const entry = videos.get(job.tab_id);
    if (job.enabled && (!entry || entry.video.dubbing_protocol !== 1 || !sameCaptionVideo(entry.video, job.video) ||
        entry.documentStart !== job.document_start)) {
      await post("/v1/dubbing", {id: job.id, tab_id: job.tab_id, video: job.video,
        document_start: job.document_start, lease_id: job.lease_id, status: "error", reason: "video_changed"}).catch(() => {});
      return;
    }
    dubbingRequests.set(job.id, {...job, started: Date.now()});
    dubbingLeases.set(job.lease_id, {...job, started: Date.now()});
    try {
      const reply = await chrome.tabs.sendMessage(job.tab_id, {to: [], kind: "trancy-desktop-dubbing-request-v1",
        request_id: job.id, lease_id: job.lease_id, document_start: job.document_start, video: job.video,
        enabled: job.enabled}, {frameId: 0});
      if (!reply?.accepted) throw new Error("dubbing observer unavailable");
    } catch (_) {
      dubbingRequests.delete(job.id);
      dubbingLeases.delete(job.lease_id);
      await post("/v1/dubbing", {id: job.id, tab_id: job.tab_id, video: job.video,
        document_start: job.document_start, lease_id: job.lease_id, status: "error", reason: "observer_unavailable"}).catch(() => {});
    }
  }

  async function dubbingResult(message, sender) {
    const lease = dubbingLeases.get(message.lease_id), request = dubbingRequests.get(message.request_id);
    const entry = videos.get(sender.tab.id);
    if (!lease || lease.tab_id !== sender.tab.id || message.document_start !== lease.document_start ||
        !sameCaptionVideo(message.video, lease.video) || !dubbingStatuses.has(message.status) ||
        !/^[a-f0-9]{32}$/.test(message.request_id || "")) return;
    if (message.status === "active" && (!request || !request.enabled || !entry ||
        entry.documentStart !== lease.document_start || !sameCaptionVideo(entry.video, lease.video))) return;
    dubbingRequests.delete(message.request_id);
    if (message.status !== "active") {
      dubbingLeases.delete(message.lease_id);
      for (const [id, job] of dubbingRequests) if (job.lease_id === message.lease_id) dubbingRequests.delete(id);
    }
    await post("/v1/dubbing", {id: message.request_id, tab_id: lease.tab_id, video: lease.video,
      document_start: lease.document_start, lease_id: lease.lease_id, status: message.status,
      reason: dubbingReasons.has(message.reason) ? message.reason : "",
      position: Math.max(0, Math.min(604800, Number(message.position) || 0)), paused: Boolean(message.paused),
      ended: message.ended === true,
      rate: Math.max(.1, Math.min(16, Number(message.rate) || 1)), seeking: Boolean(message.seeking),
      buffering: Boolean(message.buffering),
      seek_serial: Number.isSafeInteger(message.seek_serial) && message.seek_serial >= 0 ? message.seek_serial : 0}).catch(() => {});
  }

  function releaseDubbing(tabId = null) {
    for (const [leaseId, lease] of dubbingLeases) {
      if (tabId !== null && lease.tab_id !== tabId) continue;
      dubbingLeases.delete(leaseId);
      for (const [id, job] of dubbingRequests) if (job.lease_id === leaseId) dubbingRequests.delete(id);
      // Best effort immediate release; the page's independent 3-second lease
      // also works when this worker, Chrome or the desktop process disappears.
      void chrome.tabs.sendMessage(lease.tab_id, {to: [], kind: "trancy-desktop-dubbing-request-v1",
        request_id: crypto.randomUUID().replaceAll("-", ""), lease_id: leaseId,
        document_start: lease.document_start, video: lease.video, enabled: false,
        reason: "bridge_disconnected"}, {frameId: 0}).catch(() => {});
    }
  }

  async function publishVideo(tabId, entry) {
    if (!session || videos.get(tabId) !== entry) return;
    entry.dirty = false;
    await post("/v1/video", {tab_id: tabId, seq: nextSequence(tabId), video: entry.video});
  }

  async function captionJob(job) {
    if (!job || !/^[a-f0-9]{32}$/.test(job.id || "") || !Number.isSafeInteger(job.tab_id) ||
        captionRequests.has(job.id)) return;
    const entry = videos.get(job.tab_id);
    if (!entry || !sameCaptionVideo(entry.video, job.video) || entry.documentStart !== job.document_start) {
      await post("/v1/captions", {id: job.id, tab_id: job.tab_id, video: job.video,
        document_start: job.document_start, status: "error", error_code: "video_changed"}).catch(() => {});
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
        document_start: job.document_start, status: "error", error_code: "observer_unavailable"}).catch(() => {});
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
      const diagnostic = {};
      if (captionErrorCodes.has(message.error_code)) diagnostic.error_code = message.error_code;
      if (typeof message.native_track_available === "boolean") diagnostic.native_track_available = message.native_track_available;
      await post("/v1/captions", {...base, status: message.status, ...diagnostic}).catch(() => {});
      return;
    }
    if (message.status !== "ready" || !Array.isArray(message.cues) || !message.cues.length || message.cues.length > 20000) {
      await post("/v1/captions", {...base, status: "error", error_code: "invalid_caption_payload"}).catch(() => {});
      return;
    }
    // Only public subtitle text/times are forwarded. Even a spoofed page event
    // cannot smuggle its fetch headers, signed URLs or unrelated JSON fields.
    const chunks = []; let chunk = [], size = 0, failureCode = "invalid_caption_payload";
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
      failureCode = "bridge_upload_failed";
      for (let i = 0; i < chunks.length; i++) {
        const current = videos.get(job.tab_id);
        if (!current || current.documentStart !== job.document_start || !sameCaptionVideo(current.video, job.video)) return;
        const reply = await post("/v1/captions", {...base, status: "ready", part: i, part_count: chunks.length, cues: chunks[i],
          native_track_available: true,
          source_language: String(message.source_language || "").slice(0, 32),
          caption_source: ["youtube-human", "youtube-asr", "bilibili-human", "bilibili-asr"].includes(message.caption_source) ? message.caption_source : ""});
        if (reply.accepted === false) return; // Stopped/replaced request: no remaining text uploads.
      }
    } catch (_) {
      await post("/v1/captions", {...base, status: "error", error_code: failureCode}).catch(() => {});
    }
  }

  async function closeTab(tabId) {
    releaseDubbing(tabId);
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
      if (!session) session = (await post("/v1/hello", {connector_version: chrome.runtime.getManifest().version || ""}, false)).session;
      const reply = await post("/v1/poll", {});
      connected = true;
      for (const job of reply.jobs || []) void translateJob(job);
      for (const job of reply.caption_jobs || []) void captionJob(job);
      for (const job of reply.dubbing_jobs || []) void dubbingJob(job);
      const now = Date.now();
      for (const [id, job] of captionRequests) if (now - job.started > 15000) captionRequests.delete(id);
      for (const [id, job] of dubbingRequests) if (now - job.started > 5000) dubbingRequests.delete(id);
      for (const [tabId, entry] of videos) {
        if (now - entry.seen > 5000) { await closeTab(tabId); continue; }
        if (!entry.dirty) continue;
        await publishVideo(tabId, entry);
      }
      if (now - lastKeepalive > 20000) {
        lastKeepalive = now;
        // Only while a desktop app is connected. A Chrome API event keeps this
        // MV3 worker available for pending video subtitles and translations.
        await chrome.runtime.getPlatformInfo();
      }
    } catch (_) {
      releaseDubbing();
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
        if (!["trancy-desktop-report-v1", "trancy-desktop-captions-report-v1", "trancy-desktop-dubbing-report-v1"].includes(message?.kind) || sender.id !== chrome.runtime.id ||
            sender.frameId !== 0 || !sender.tab || !pageIdentity(sender.tab.url) ||
            pageIdentity(message.href) !== pageIdentity(sender.tab.url)) return;
        if (message.kind === "trancy-desktop-captions-report-v1") { void captionResult(message, sender); return; }
        if (message.kind === "trancy-desktop-dubbing-report-v1") { void dubbingResult(message, sender); return; }
        const previous = reports.get(sender.tab.id);
        const docStart = Number(message.document_start), sequence = Number(message.sequence);
        if (!Number.isFinite(docStart) || !Number.isSafeInteger(sequence) ||
            (previous && (docStart < previous.documentStart || (docStart === previous.documentStart && sequence <= previous.sequence)))) return;
        reports.set(sender.tab.id, {documentStart: docStart, sequence});
        if (message.video === null) { void closeTab(sender.tab.id); return; }
        const video = sanitizeVideo({...message.video, document_start: docStart}, sender);
        if (!video) return;
        const priorVideo = videos.get(sender.tab.id);
        if (priorVideo && (priorVideo.documentStart !== docStart || !sameCaptionVideo(priorVideo.video, video))) releaseDubbing(sender.tab.id);
        const entry = {video, documentStart: docStart, sequence, seen: Date.now(), dirty: true};
        videos.set(sender.tab.id, entry);
        if (video.playback_event) void publishVideo(sender.tab.id, entry).catch(() => {releaseDubbing(); session = "";});
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
