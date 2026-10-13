/* MAIN-world observer + requested native text tracks. No media download or ASR. */
(() => {
  "use strict";
  if (window !== window.top || window.__trancyDesktopObserver) return;
  window.__trancyDesktopObserver = true;
  const MESSAGE = "trancy-desktop-video-v1";
  const docStart = performance.timeOrigin;
  const initialHref = location.href;
  const biliCaptures = new Map();
  const biliSubtitleCaptures = new Map();
  const biliSubtitleBodies = new Map();
  const youtubeCaptures = new Map();
  const captionCache = new Map();
  const captionRequests = new Map();
  const siteFetch = window.fetch;
  let sequence = 0;
  let requestSequence = 0;
  let dubbingLease = null;
  let watchedVideo = null;
  const retiredDubbingLeases = new Set();
  const playbackEvents = ["play", "pause", "ended", "seeking", "seeked", "waiting", "stalled", "playing", "ratechange", "loadedmetadata", "emptied"];
  const playbackHandlers = new Map();
  let waitingForPlayback = false;
  let seekSerial = 0;

  function pageIdentity(href = location.href) {
    try {
      const url = new URL(href);
      if (url.protocol !== "https:") return null;
      if (["www.youtube.com", "youtube.com", "m.youtube.com", "www.youtube-nocookie.com"].includes(url.hostname)) {
        const match = url.pathname.match(/^\/(?:embed|live|shorts)\/([A-Za-z0-9_-]{11})\/?$/);
        const id = url.pathname === "/watch" ? url.searchParams.get("v") : match?.[1];
        if (!/^[A-Za-z0-9_-]{11}$/.test(id || "")) return null;
        return {platform: "youtube", video_id: id, page: 1, url: `https://www.youtube.com/watch?v=${id}`};
      }
      if (url.hostname === "bilibili.com" || url.hostname.endsWith(".bilibili.com")) {
        const match = url.pathname.match(/^\/video\/(BV[0-9A-Za-z]{10})\/?$/);
        const page = Number(url.searchParams.get("p") || 1);
        if (!match || !Number.isInteger(page) || page < 1 || page > 10000) return null;
        return {platform: "bilibili", video_id: match[1], bvid: match[1], page,
          url: `https://www.bilibili.com/video/${match[1]}/?p=${page}`};
      }
    } catch (_) { /* Unsupported page. */ }
    return null;
  }

  function validMedia(url, platform, cid = "") {
    try {
      const value = new URL(url);
      if (value.protocol !== "https:" || value.username || value.password || (value.port && value.port !== "443")) return false;
      const domains = platform === "youtube" ? ["googlevideo.com"] : ["bilivideo.com", "bilivideo.cn", "hdslb.com", "bilibili.com"];
      if (!domains.some(d => value.hostname === d || value.hostname.endsWith(`.${d}`))) return false;
      return platform === "youtube" || (cid && new RegExp(`(^|[^0-9])${cid}([^0-9]|$)`).test(value.pathname));
    } catch (_) { return false; }
  }

  function biliAudio(data, cid) {
    const tracks = [...(data?.dash?.audio || [])].sort((a, b) => (b.bandwidth || 0) - (a.bandwidth || 0));
    const urls = tracks.flatMap(t => [t.baseUrl || t.base_url, ...(t.backupUrl || t.backup_url || [])]);
    // Only a single complete media file can stand in for an independent audio track.
    if (!tracks.length && data?.durl?.length === 1) urls.push(data.durl[0].url);
    return [...new Set(urls.filter(u => typeof u === "string" && validMedia(u, "bilibili", cid)))].slice(0, 12);
  }

  function biliRequestIdentity() {
    const identity = pageIdentity();
    if (!identity || identity.platform !== "bilibili") return identity;
    const state = window.__INITIAL_STATE__?.videoData;
    const selected = state?.bvid === identity.video_id ? state.pages?.find(p => Number(p.page) === identity.page) : null;
    const first = pageIdentity(initialHref);
    const initialCid = state?.bvid === identity.video_id && first?.video_id === identity.video_id && first?.page === identity.page ? state?.cid : "";
    const cid = selected?.cid || biliCaptures.get(`${identity.video_id}:${identity.page}`)?.cid || initialCid;
    return {...identity, cid: /^[1-9][0-9]{0,19}$/.test(String(cid || "")) ? String(cid) : ""};
  }

  function inspectBiliResponse(requestUrl, payload, requestedPage, ordinal = 0) {
    try {
      const url = new URL(requestUrl, location.href);
      const current = pageIdentity();
      if (!current || current.platform !== "bilibili" || url.hostname !== "api.bilibili.com" ||
          !/^\/x\/player\/(?:wbi\/)?(?:playurl|v2)$/.test(url.pathname)) return;
      if (!requestedPage || requestedPage.url !== current.url) return;
      const cid = url.searchParams.get("cid");
      if (!/^[1-9][0-9]{0,19}$/.test(cid || "")) return;
      const state = window.__INITIAL_STATE__?.videoData;
      const requestedAid = url.searchParams.get("aid") || url.searchParams.get("avid");
      const bvid = url.searchParams.get("bvid") || (requestedAid && String(state?.aid) === requestedAid ? state?.bvid : "");
      if (bvid !== current.video_id) return;
      if (requestedAid && state?.bvid === bvid && String(state.aid) !== requestedAid) return;
      const pageInfo = state?.bvid === bvid ? state.pages?.find(p => Number(p.page) === current.page) : null;
      if (pageInfo && String(pageInfo.cid) !== cid) return;
      if (requestedPage.cid && requestedPage.cid !== cid) return;
      if (url.pathname.endsWith("/v2")) {
        const key = `${bvid}:${current.page}:${cid}`;
        const previous = biliSubtitleCaptures.get(key);
        if (previous && previous.ordinal > ordinal) return;
        if (Array.isArray(payload.data?.subtitle?.subtitles)) {
          if (payload?.code !== 0) return;
          biliSubtitleCaptures.set(key, {tracks: payload.data.subtitle.subtitles, provenance: "native", ordinal});
        } else if (payload && typeof payload.code === "number" && payload.code !== 0) {
          // A failed authenticated/native request cannot prove that no track exists.
          // It must not invalidate a previously successful track/body binding.
          if (!Array.isArray(previous?.tracks) || !previous.tracks.length) {
            biliSubtitleCaptures.set(key, {tracks: null, provenance: "native", ordinal});
          }
        }
        while (biliSubtitleCaptures.size > 8) biliSubtitleCaptures.delete(biliSubtitleCaptures.keys().next().value);
        return;
      }
      if (payload?.code !== 0) return;
      biliCaptures.set(`${bvid}:${current.page}`, {cid, audio: biliAudio(payload.data, cid)});
      while (biliCaptures.size > 8) biliCaptures.delete(biliCaptures.keys().next().value);
    } catch (_) { /* Never interfere with the site's request. */ }
  }

  function inspectBiliSubtitle(requestUrl, payload, requestedPage, ordinal = 0) {
    try {
      const url = biliCaptionUrl(requestUrl), current = biliRequestIdentity();
      if (!requestedPage?.cid || !current?.cid || nativeIdentity(requestedPage) !== nativeIdentity(current) ||
          !Array.isArray(payload?.body) || !payload.body.length || payload.body.length > 20000) return;
      const body = payload.body.filter(c => c && typeof c.content === "string" && c.content.trim() && c.content.length <= 16000 &&
        Number.isFinite(c.from) && Number.isFinite(c.to) && 0 <= c.from && c.from < c.to && c.to <= 604800)
        .map(c => ({from: c.from, to: c.to, content: c.content}));
      if (!body.length || body.reduce((n, c) => n + c.content.length, 0) > 2000000) return;
      // A CDN URL alone contains no CID. Keep a request-time snapshot, and use
      // this body only after its URL is also present in metadata for this exact
      // BV / page / CID. Preloaded next-video bodies cannot become current cues.
      const previous = biliSubtitleBodies.get(url);
      if (previous?.identity === nativeIdentity(current) && previous.ordinal > ordinal) return;
      biliSubtitleBodies.set(url, {identity: nativeIdentity(current), payload: {body}, ordinal});
      while (biliSubtitleBodies.size > 12) biliSubtitleBodies.delete(biliSubtitleBodies.keys().next().value);
    } catch (_) { /* Ignore other assets and foreign/unbound subtitle URLs. */ }
  }

  function timedtextUrl(raw) {
    try {
      const url = new URL(raw, location.href);
      return url.protocol === "https:" && !url.username && !url.password && (!url.port || url.port === "443") &&
        ["www.youtube.com", "youtube.com", "m.youtube.com", "www.youtube-nocookie.com"].includes(url.hostname) &&
        url.pathname === "/api/timedtext" && /^[A-Za-z0-9_-]{11}$/.test(url.searchParams.get("v") || "") ? url : null;
    } catch (_) { return null; }
  }

  function inspectYoutubeResponse(requestUrl, payload, requestedPage) {
    const url = timedtextUrl(requestUrl), current = pageIdentity();
    if (!url || !current || current.platform !== "youtube" || !requestedPage || requestedPage.url !== current.url ||
        url.searchParams.get("v") !== current.video_id) return;
    // These request parameters (including native proof tokens, if present) stay
    // exclusively in this page. The desktop receives only parsed text/times.
    youtubeCaptures.set(url.href, {url: url.href, payload: Array.isArray(payload?.events) ? payload : null});
    while (youtubeCaptures.size > 12) youtubeCaptures.delete(youtubeCaptures.keys().next().value);
  }

  function inspectResponse(url, payload, identity, ordinal = 0) {
    if (identity?.platform === "bilibili") {
      if (/\/bfs\/(?:ai_)?subtitle\//.test(url)) inspectBiliSubtitle(url, payload, identity, ordinal);
      else inspectBiliResponse(url, payload, identity, ordinal);
    }
    else if (identity?.platform === "youtube") inspectYoutubeResponse(url, payload, identity);
  }

  // Observe only the two sites' playback metadata / native subtitle endpoints.
  if (pageIdentity() || ["www.youtube.com", "youtube.com", "m.youtube.com"].includes(location.hostname) ||
      location.hostname === "bilibili.com" || location.hostname.endsWith(".bilibili.com")) {
    const originalFetch = window.fetch;
    window.fetch = function (...args) {
      const requestUrl = typeof args[0] === "string" ? args[0] : args[0] instanceof URL ? args[0].href : args[0]?.url;
      const requestedPage = biliRequestIdentity(), ordinal = ++requestSequence;
      return originalFetch.apply(this, args).then(response => {
        if (typeof requestUrl === "string" && (/\/(?:playurl|v2|api\/timedtext)(?:\?|$)/.test(requestUrl) || /\/bfs\/(?:ai_)?subtitle\//.test(requestUrl))) {
          response.clone().json().then(data => inspectResponse(requestUrl, data, requestedPage, ordinal))
            .catch(() => inspectResponse(requestUrl, null, requestedPage, ordinal));
        }
        return response;
      });
    };
    const originalOpen = XMLHttpRequest.prototype.open;
    const xhrRequests = new WeakMap();
    const observedRequests = new WeakSet();
    XMLHttpRequest.prototype.open = function (method, url, ...rest) {
      xhrRequests.set(this, {url: String(url), page: biliRequestIdentity(), ordinal: ++requestSequence});
      if (!observedRequests.has(this)) {
        observedRequests.add(this);
        this.addEventListener("load", () => {
          try {
            const request = xhrRequests.get(this);
            if (!request || (!/\/(?:playurl|v2|api\/timedtext)(?:\?|$)/.test(request.url) && !/\/bfs\/(?:ai_)?subtitle\//.test(request.url))) return;
            let payload = null;
            try { payload = this.responseType === "json" ? this.response : JSON.parse(this.responseText); } catch (_) { /* Keep native URL context. */ }
            // Metadata binds the original requested CDN URL; a signed redirect
            // does not change the current subtitle track's identity.
            inspectResponse(/\/bfs\/(?:ai_)?subtitle\//.test(request.url) ? request.url : this.responseURL || request.url,
              payload, request.page, request.ordinal);
          } catch (_) { /* Unrelated or unreadable response. */ }
        });
      }
      return originalOpen.call(this, method, url, ...rest);
    };
  }

  function captionLanguage(raw) {
    const value = String(raw || "").replace(/^ai-/, "").toLowerCase();
    if (value === "en" || value.startsWith("en-")) return "en";
    if (value === "zh-tw" || value === "zh-hant" || value === "zh-hk") return "zh-TW";
    if (value === "zh" || value.startsWith("zh-") || value === "cmn") return "zh-CN";
    return value.slice(0, 32);
  }

  const sameLanguage = (a, b) => a === b || (String(a).startsWith("zh") && String(b).startsWith("zh"));
  const oppositeLanguage = language => language === "en" ? "zh-CN" : language.startsWith("zh") ? "en" : "";
  const nativeIdentity = video => video ? `${video.platform}:${video.video_id}:${video.page}:${video.cid || ""}` : "";

  function stillCurrent(video) {
    const current = pageIdentity();
    const found = current && currentVideo(current);
    return found && nativeIdentity(found) === nativeIdentity(video);
  }

  async function fetchJson(url, signal, credentials = "include", timeout = 4500) {
    const controller = new AbortController();
    const abort = () => controller.abort();
    if (signal?.aborted) controller.abort();
    signal?.addEventListener("abort", abort, {once: true});
    const timer = setTimeout(abort, timeout);
    try {
      const response = await siteFetch.call(window, url, {credentials, cache: "no-store", signal: controller.signal});
      if (!response.ok) throw new Error("native caption fetch failed");
      return await response.json();
    } finally {
      clearTimeout(timer); signal?.removeEventListener("abort", abort);
    }
  }

  function youtubeCues(payload, language, kind) {
    if (!Array.isArray(payload?.events)) throw new Error("invalid JSON3 captions");
    const events = payload.events.filter(e => e && Array.isArray(e.segs) && e.segs.some(s => typeof s.utf8 === "string" && s.utf8.trim()) &&
      Number.isFinite(e.tStartMs) && e.tStartMs >= 0).sort((a, b) => a.tStartMs - b.tStartMs);
    const cues = [];
    for (let index = 0; index < events.length; index++) {
      const event = events[index], next = events[index + 1];
      const start = event.tStartMs / 1000;
      let end = start + (Number(event.dDurationMs) > 0 ? Number(event.dDurationMs) / 1000 : 5);
      // YouTube ASR uses rolling windows: close the current text when the next
      // event replaces it, while retaining real segment offsets for word follow.
      if (next && next.tStartMs > event.tStartMs) end = Math.min(end, next.tStartMs / 1000);
      end = Math.min(end, 604800);
      const text = event.segs.map(s => typeof s.utf8 === "string" ? s.utf8 : "").join("").trim();
      if (!text || text.length > 16000 || !(end > start)) continue;
      const cue = {start, end, text, source_language: language, subtitle_kind: kind};
      const segments = event.segs.filter(s => typeof s.utf8 === "string" && s.utf8.trim());
      if (segments.length > 1 && segments.some(s => Number(s.tOffsetMs) > 0)) {
        cue.words = segments.map((segment, i) => {
          const ws = Math.max(start, start + (Number(segment.tOffsetMs) || 0) / 1000);
          const following = segments.slice(i + 1).find(s => Number(s.tOffsetMs) > (Number(segment.tOffsetMs) || 0));
          const we = following ? Math.min(end, start + Number(following.tOffsetMs) / 1000) : end;
          return {text: segment.utf8, start: ws, end: we};
        }).filter(w => w.start < w.end && w.end <= end);
      }
      // aAppend is a continuation rather than a replacement rolling window.
      const previous = cues.at(-1);
      if (event.aAppend === 1 && previous && start - previous.end <= .3 && previous.source_language === language) {
        previous.text += text.startsWith(" ") || previous.text.endsWith(" ") ? text : ` ${text}`;
        previous.end = Math.max(previous.end, end);
        if (previous.words && cue.words) previous.words.push(...cue.words);
        else delete previous.words;
      } else cues.push(cue);
    }
    if (!cues.length || cues.length > 20000) throw new Error("empty native caption body");
    return cues;
  }

  function biliCues(payload, language, kind) {
    if (!Array.isArray(payload?.body)) throw new Error("invalid Bilibili captions");
    const cues = payload.body.filter(c => c && typeof c.content === "string" && c.content.trim() && c.content.length <= 16000 &&
      Number.isFinite(c.from) && Number.isFinite(c.to) && 0 <= c.from && c.from < c.to && c.to <= 604800)
      .map(c => ({start: c.from, end: c.to, text: c.content.trim(), source_language: language, subtitle_kind: kind}))
      .sort((a, b) => a.start - b.start || a.end - b.end);
    if (!cues.length || cues.length > 20000) throw new Error("empty native caption body");
    return cues;
  }

  function mergeNativeTranslation(original, translated, target) {
    let lower = 0;
    for (const cue of original) {
      while (lower < translated.length && translated[lower].end <= cue.start) lower++;
      const matches = [];
      for (let i = lower; i < translated.length && translated[i].start < cue.end; i++) {
        const other = translated[i];
        if (Math.min(cue.end, other.end) - Math.max(cue.start, other.start) > .04) matches.push(other.text);
      }
      if (matches.length) {cue.translation = [...new Set(matches)].join(" "); cue.target_language = target;}
    }
    return original;
  }

  function chooseTrack(tracks, preferred, sourceHint = "") {
    const language = captionLanguage(preferred);
    const hint = language && language !== "auto" ? language : captionLanguage(sourceHint) || tracks[0]?.language;
    const same = tracks.filter(t => sameLanguage(t.language, hint));
    return same.find(t => t.kind === "human") || same[0] || tracks.find(t => t.kind === "human" &&
      (t.language === "en" || t.language.startsWith("zh"))) || tracks.find(t => t.language === "en" || t.language.startsWith("zh")) || tracks[0];
  }

  function capturedYoutube(track, video, requireBody = false) {
    const trackUrl = timedtextUrl(track.baseUrl);
    const name = trackUrl?.searchParams.get("name") || "";
    return [...youtubeCaptures.values()].reverse().find(capture => {
      const url = timedtextUrl(capture.url);
      return url && url.searchParams.get("v") === video.video_id && !url.searchParams.get("tlang") &&
        url.searchParams.get("lang") === track.languageCode &&
        (url.searchParams.get("kind") || "") === (track.kind === "asr" ? "asr" : "") &&
        (url.searchParams.get("name") || "") === name && (!requireBody || capture.payload);
    });
  }

  function youtubeCaptionUrl(track, video) {
    const captured = capturedYoutube(track, video);
    const url = timedtextUrl(captured?.url || track.baseUrl);
    if (!url || url.searchParams.get("v") !== video.video_id) throw new Error("unbound caption URL");
    url.searchParams.set("fmt", "json3"); url.searchParams.delete("tlang");
    // Match the existing plugin's device/player context preparation when no
    // captured native timedtext URL is available yet.
    if (!captured) {
      try {
        const device = new URLSearchParams(window.ytcfg?.get?.("DEVICE") || "");
        for (const name of ["cbr", "cbrand", "cbrver", "cos", "cosver", "cplatform"]) {
          if (device.get(name)) url.searchParams.set(name, device.get(name));
        }
        const player = document.querySelector("#movie_player") || document.querySelector("#player");
        const config = player?.getWebPlayerContextConfig?.();
        if (config?.innertubeContextClientVersion) url.searchParams.set("cver", config.innertubeContextClientVersion);
        for (const [key, value] of Object.entries({c: "WEB", cplayer: "UNIPLAYER", xorb: "2", xobt: "3", xovt: "3"})) url.searchParams.set(key, value);
      } catch (_) { /* A complete baseUrl may already suffice. */ }
    }
    return url.href;
  }

  async function downloadYoutubeTrack(track, video, signal, deadline) {
    const cached = capturedYoutube(track, video, true);
    if (cached) return youtubeCues(cached.payload, track.language, track.captionKind);
    const player = document.querySelector("#movie_player") || document.querySelector("#player");
    let previous, selected = false;
    try {
      // Loading a native track obtains the site's normal request context. No
      // undocumented proof generation, audio extraction or cloud ASR is used.
      try {
        previous = player?.getOption?.("captions", "track");
        player?.loadModule?.("captions");
        if (typeof player?.setOption === "function") {
          player.setOption("captions", "track", {languageCode: track.languageCode, kind: track.kind || "",
            ...(track.vssId ? {vss_id: track.vssId} : {})}); selected = true;
        }
      } catch (_) { /* Try a valid original track URL. */ }
      try {
        const payload = await fetchJson(youtubeCaptionUrl(track, video), signal);
        return youtubeCues(payload, track.language, track.captionKind);
      } catch (_) { /* Native player requests may have a newer signed context. */ }
      let retried = "";
      while (!signal.aborted && Date.now() < deadline && stillCurrent(video)) {
        const native = capturedYoutube(track, video, true);
        if (native) return youtubeCues(native.payload, track.language, track.captionKind);
        const context = capturedYoutube(track, video);
        if (context && context.url !== retried) {
          retried = context.url;
          try { return youtubeCues(await fetchJson(youtubeCaptionUrl(track, video), signal, "include", 2000), track.language, track.captionKind); }
          catch (_) { /* Keep waiting for native JSON3 within this request's budget. */ }
        }
        await new Promise(resolve => setTimeout(resolve, 150));
      }
      throw new Error("native YouTube caption unavailable");
    } finally {
      // Preserve a user-selected native track when its value can be observed.
      if (selected && previous && stillCurrent(video)) {
        try {
          const current = player?.getOption?.("captions", "track");
          if (current?.languageCode === track.languageCode) player.setOption("captions", "track", previous);
        } catch (_) { /* Subtitle restoration must not break playback. */ }
      }
    }
  }

  function biliCaptionUrl(raw) {
    const url = new URL(raw, location.href);
    if (url.protocol !== "https:" || url.username || url.password || (url.port && url.port !== "443") ||
        !["hdslb.com", "bilibili.com"].some(d => url.hostname === d || url.hostname.endsWith(`.${d}`)) ||
        !/^\/bfs\/(?:ai_)?subtitle\//.test(url.pathname)) throw new Error("unbound Bilibili subtitle URL");
    return url.href;
  }

  function captionFailure(code, available) {
    const error = new Error(code); error.captionCode = code;
    if (typeof available === "boolean") error.nativeTrackAvailable = available;
    return error;
  }

  function biliTrackBody(track, video) {
    try {
      const capture = biliSubtitleBodies.get(biliCaptionUrl(track.subtitle_url));
      return capture?.identity === nativeIdentity(video) ? capture : null;
    } catch (_) { return null; }
  }

  async function biliTracks(video, signal) {
    const key = `${video.video_id}:${video.page}:${video.cid}`;
    const existing = biliSubtitleCaptures.get(key);
    if (Array.isArray(existing?.tracks)) return existing.tracks;
    // A native wbi/v2 response may still be in flight. Race its normal page
    // response against one unsigned, legacy metadata request; do not sign WBI
    // or export credentials. An empty legacy response alone is inconclusive.
    const legacyController = new AbortController();
    const abort = () => legacyController.abort();
    if (signal.aborted) abort();
    signal.addEventListener("abort", abort, {once: true});
    const started = Date.now();
    let legacy = null, completed = 0;
    const url = new URL("https://api.bilibili.com/x/player/v2");
    url.searchParams.set("bvid", video.video_id); url.searchParams.set("cid", video.cid);
    const state = window.__INITIAL_STATE__?.videoData;
    if (state?.bvid === video.video_id && /^[1-9][0-9]{0,19}$/.test(String(state.aid || ""))) url.searchParams.set("aid", String(state.aid));
    void fetchJson(url.href, legacyController.signal).then(payload => {
      if (payload?.code === 0 && Array.isArray(payload.data?.subtitle?.subtitles)) legacy = payload.data.subtitle.subtitles;
      completed = Date.now();
    }).catch(() => {completed = Date.now();});
    try {
      while (!signal.aborted && stillCurrent(video) && Date.now() - started < 6500) {
        const native = biliSubtitleCaptures.get(key);
        if (Array.isArray(native?.tracks)) return native.tracks;
        if (legacy?.length) {
          biliSubtitleCaptures.set(key, {tracks: legacy, provenance: "legacy", ordinal: 0});
          while (biliSubtitleCaptures.size > 8) biliSubtitleCaptures.delete(biliSubtitleCaptures.keys().next().value);
          return legacy;
        }
        if (completed && Date.now() >= Math.max(started + 1800, completed + 600)) break;
        await new Promise(resolve => setTimeout(resolve, 120));
      }
      throw captionFailure("bili_metadata_failed");
    } finally {
      legacyController.abort(); signal.removeEventListener("abort", abort);
    }
  }

  async function downloadBiliTrack(track, video, signal, timeout = 4500) {
    const cached = biliTrackBody(track, video);
    if (cached) return biliCues(cached.payload, track.language, track.kind);
    try {
      const url = biliCaptionUrl(track.subtitle_url);
      const payload = await fetchJson(url, signal, "omit", timeout);
      if (!stillCurrent(video)) throw new Error("video changed");
      const cues = biliCues(payload, track.language, track.kind);
      inspectBiliSubtitle(url, payload, biliRequestIdentity(), ++requestSequence);
      return cues;
    } catch (_) {
      // The page may have loaded this same chosen track during our request.
      const arrived = biliTrackBody(track, video);
      if (arrived) return biliCues(arrived.payload, track.language, track.kind);
      throw captionFailure("bili_track_failed", true);
    }
  }

  async function loadNativeCaptions(video, preferred, signal) {
    const cacheKey = `${nativeIdentity(video)}:${captionLanguage(preferred)}`;
    if (captionCache.has(cacheKey)) return captionCache.get(cacheKey);
    let tracks, download, sourceHint = "", cachedPrimary = null;
    if (video.platform === "youtube") {
      const player = document.querySelector("#movie_player") || document.querySelector("#player");
      const response = player?.getPlayerResponse?.() || window.ytInitialPlayerResponse;
      if (response?.videoDetails?.videoId !== video.video_id) throw captionFailure("youtube_metadata_failed");
      const nativeTracks = response.captions?.playerCaptionsTracklistRenderer?.captionTracks;
      if (!Array.isArray(nativeTracks) || !nativeTracks.length) return {status: "unavailable", cues: [], native_track_available: false};
      tracks = nativeTracks.filter(t => timedtextUrl(t.baseUrl)?.searchParams.get("v") === video.video_id)
        .map(t => ({...t, language: captionLanguage(t.languageCode), captionKind: t.kind === "asr" ? "asr" : "human",
          kind: t.kind === "asr" ? "asr" : "human"}));
      // Preserve the endpoint's `kind` separately; "human" is not a timedtext kind.
      tracks = tracks.map(t => ({...t, nativeKind: t.kind === "asr" ? "asr" : "", kind: t.kind}));
      sourceHint = nativeTracks.find(t => t.kind === "asr")?.languageCode || "";
      download = track => downloadYoutubeTrack({...track, kind: track.nativeKind}, video, signal, Date.now() + 7000);
    } else {
      if (!/^[1-9][0-9]{0,19}$/.test(video.cid || "")) throw captionFailure("bili_metadata_failed");
      const nativeTracks = await biliTracks(video, signal);
      if (!nativeTracks.length) return {status: "unavailable", cues: [], native_track_available: false};
      tracks = nativeTracks.filter(t => typeof t.subtitle_url === "string").map(t => ({...t,
        language: captionLanguage(t.lan), kind: String(t.lan).startsWith("ai-") ? "asr" : "human"}));
      // Reuse the track already chosen and loaded by the native player. Its
      // language comes from this video's validated track list, not from a URL.
      const preferredLanguage = captionLanguage(preferred);
      const loaded = tracks.map(t => ({track: t, body: biliTrackBody(t, video)})).filter(t => t.body &&
        (!preferredLanguage || preferredLanguage === "auto" || sameLanguage(t.track.language, preferredLanguage)))
        .sort((a, b) => b.body.ordinal - a.body.ordinal)[0];
      cachedPrimary = loaded?.track || null;
      sourceHint = cachedPrimary?.language || "";
      download = track => downloadBiliTrack(track, video, signal);
    }
    if (!tracks.length) throw captionFailure(video.platform === "bilibili" ? "bili_track_failed" : "youtube_track_failed", true);
    const primary = cachedPrimary || chooseTrack(tracks, preferred, sourceHint);
    const target = oppositeLanguage(primary.language);
    const secondary = target && tracks.find(t => sameLanguage(t.language, target) && t.kind === "human");
    // Obtain original text first. Optional target failure never discards it.
    let cues;
    try { cues = await download(primary); }
    catch (error) { throw error?.captionCode ? error : captionFailure(video.platform === "bilibili" ? "bili_track_failed" : "youtube_track_failed", true); }
    if (!stillCurrent(video) || signal.aborted) throw captionFailure("video_changed", true);
    if (secondary) {
      const secondaryController = new AbortController();
      const timer = setTimeout(() => secondaryController.abort(), 1000);
      try {
        // Native target retrieval is best effort; Google can fill any remaining
        // lines immediately instead of waiting for the complete second track.
        let translated;
        if (video.platform === "bilibili") translated = await downloadBiliTrack(secondary, video, secondaryController.signal, 1000);
        else translated = youtubeCues(await fetchJson(youtubeCaptionUrl({...secondary, kind: secondary.nativeKind}, video), secondaryController.signal, "include", 1000), secondary.language, secondary.captionKind);
        mergeNativeTranslation(cues, translated, secondary.language);
      } catch (_) { /* Retain the usable original track. */ }
      finally { clearTimeout(timer); }
    }
    const result = {status: "ready", cues, source_language: primary.language,
      caption_source: `${video.platform}-${primary.captionKind || primary.kind}`, native_track_available: true};
    captionCache.set(cacheKey, result);
    while (captionCache.size > 8) captionCache.delete(captionCache.keys().next().value);
    return result;
  }

  window.addEventListener("message", event => {
    const message = event.data;
    if (event.source !== window || event.origin !== location.origin || message?.kind !== "trancy-desktop-captions-command-v1" ||
        message.document_start !== docStart || !/^[a-f0-9]{32}$/.test(message.request_id || "") ||
        captionRequests.has(message.request_id)) return;
    const identity = pageIdentity(), found = identity && currentVideo(identity);
    if (!found || nativeIdentity(found) !== nativeIdentity(message.video)) return;
    const controller = new AbortController();
    captionRequests.set(message.request_id, {controller, identity: nativeIdentity(found)});
    const timer = setTimeout(() => controller.abort(), 9500);
    const metadata = {platform: found.platform, video_id: found.video_id, url: found.url, page: found.page, cid: found.cid};
    void loadNativeCaptions(metadata, message.preferred_language, controller.signal).catch(error => ({status: "error", cues: [],
      ...(error?.captionCode ? {error_code: error.captionCode} : {}),
      ...(typeof error?.nativeTrackAvailable === "boolean" ? {native_track_available: error.nativeTrackAvailable} : {})}))
      .then(result => {
        if (!stillCurrent(metadata)) return;
        window.postMessage({kind: "trancy-desktop-captions-v1", document_start: docStart,
          sequence: ++sequence, href: location.href, request_id: message.request_id, video: metadata, ...result}, location.origin);
      }).finally(() => {clearTimeout(timer); captionRequests.delete(message.request_id);});
  });

  function currentVideo(identity) {
    if (identity.platform === "youtube") {
      const player = document.querySelector("#movie_player") || document.querySelector("#player");
      const response = player?.getPlayerResponse?.() || window.ytInitialPlayerResponse;
      if (response?.videoDetails?.videoId !== identity.video_id) return null;
      const video = player?.querySelector("video") || document.querySelector("video.html5-main-video");
      if (!video) return null;
      const audio = (response.streamingData?.adaptiveFormats || []).filter(t => String(t.mimeType).startsWith("audio/") &&
        typeof t.url === "string" && validMedia(t.url, "youtube")).sort((a, b) => (b.bitrate || 0) - (a.bitrate || 0));
      return {...identity, video, cid: "", title: response.videoDetails.title || document.title,
        audio_urls: audio.slice(0, 8).map(t => t.url),
        is_live: response.videoDetails.isLive === true,
        ad_playing: Boolean(player?.classList.contains("ad-showing") || player?.classList.contains("ad-interrupting"))};
    }
    const video = document.querySelector(".bpx-player-video-wrap video, .bilibili-player-video video, video");
    if (!video) return null;
    const state = window.__INITIAL_STATE__?.videoData;
    const selected = state?.bvid === identity.video_id ? state.pages?.find(p => Number(p.page) === identity.page) : null;
    const captured = biliCaptures.get(`${identity.video_id}:${identity.page}`);
    let cid = selected ? String(selected.cid) : captured?.cid || "";
    // Initial page data is trusted only for the first document's exact video/P.
    const first = pageIdentity(initialHref);
    let audio = [];
    if (cid && first?.video_id === identity.video_id && first?.page === identity.page &&
        state?.bvid === identity.video_id && String(state?.cid) === cid) {
      audio = biliAudio(window.__playinfo__?.data, cid);
    }
    if (captured?.cid === cid) audio = [...new Set([...captured.audio, ...audio])];
    if (!/^[1-9][0-9]{0,19}$/.test(cid)) cid = "";
    const advert = document.querySelector(".bpx-player-ad-tip, .bilibili-player-video-ad");
    return {...identity, video, cid, title: state?.bvid === identity.video_id ? state.title : document.title,
      audio_urls: audio.slice(0, 12), ad_playing: Boolean(advert?.offsetParent), is_live: false};
  }

  function dubbingIdentity(found) {
    return {platform: found.platform, video_id: found.video_id, url: found.url, page: found.page, cid: found.cid};
  }

  function dubbingReport(lease, status, reason = "") {
    const video = lease.video;
    window.postMessage({kind: "trancy-desktop-dubbing-v1", document_start: docStart,
      sequence: ++sequence, href: location.href, request_id: lease.request_id,
      lease_id: lease.lease_id, video: lease.identity, status, reason,
      position: Number.isFinite(video?.currentTime) ? video.currentTime : 0,
      paused: Boolean(video?.paused || video?.ended), ended: video?.ended === true,
      rate: Number(video?.playbackRate) || 1,
      seeking: Boolean(video?.seeking), buffering: Boolean(waitingForPlayback || video?.readyState < 3),
      seek_serial: seekSerial}, location.origin);
  }

  function retireDubbing(leaseId) {
    retiredDubbingLeases.add(leaseId);
    while (retiredDubbingLeases.size > 64) retiredDubbingLeases.delete(retiredDubbingLeases.values().next().value);
  }

  function restoreDubbing(status = "restored", reason = "disabled", userOverride = false) {
    const lease = dubbingLease;
    if (!lease) return;
    dubbingLease = null;
    clearTimeout(lease.timer);
    retireDubbing(lease.lease_id);
    // We own only the temporary mute. Volume is never changed by the bridge.
    // A user unmute is left intact; a volume-only edit keeps their new volume
    // while removing our temporary mute before relinquishing control.
    if (lease.video.muted === true && (!userOverride || lease.video.volume !== lease.volume)) {
      lease.video.muted = lease.muted;
    }
    dubbingReport(lease, status, reason);
  }

  function checkDubbing(found) {
    if (!dubbingLease) return;
    if (!found || found.video !== dubbingLease.video || nativeIdentity(found) !== nativeIdentity(dubbingLease.identity)) {
      restoreDubbing("video_changed", "video_changed");
    } else if (Date.now() >= dubbingLease.expires) {
      restoreDubbing("expired", "lease_expired");
    } else if (found.ad_playing) {
      restoreDubbing("restored", "ad_playing");
    }
  }

  window.addEventListener("message", event => {
    const message = event.data;
    if (event.source !== window || event.origin !== location.origin || message?.kind !== "trancy-desktop-dubbing-command-v1" ||
        message.document_start !== docStart || !/^[a-f0-9]{32}$/.test(message.request_id || "") ||
        !/^[a-f0-9]{32}$/.test(message.lease_id || "") || typeof message.enabled !== "boolean" ||
        message.video?.platform !== "bilibili") return;
    const identity = pageIdentity(), found = identity && currentVideo(identity);
    const requested = {request_id: message.request_id, lease_id: message.lease_id, identity: message.video, video: found?.video};
    if (!message.enabled) {
      if (dubbingLease?.lease_id === message.lease_id && nativeIdentity(dubbingLease.identity) === nativeIdentity(message.video)) {
        dubbingLease.request_id = message.request_id;
        restoreDubbing("restored", message.reason === "bridge_disconnected" ? "bridge_disconnected" : "disabled");
      } else {
        retireDubbing(message.lease_id);
        dubbingReport(requested, "restored", "disabled");
      }
      return;
    }
    if (!found || found.platform !== "bilibili" || !found.cid || nativeIdentity(found) !== nativeIdentity(message.video) || found.ad_playing) {
      dubbingReport(requested, "error", found?.ad_playing ? "ad_playing" : "video_changed");
      return;
    }
    if (retiredDubbingLeases.has(message.lease_id)) {
      dubbingReport(requested, "error", "retired_lease");
      return;
    }
    if (dubbingLease && (dubbingLease.lease_id !== message.lease_id || dubbingLease.video !== found.video)) {
      restoreDubbing("restored", "replaced");
    }
    if (!dubbingLease) {
      dubbingLease = {...requested, identity: dubbingIdentity(found), muted: found.video.muted === true,
        volume: Number(found.video.volume), timer: null};
      found.video.muted = true;
    }
    dubbingLease.request_id = message.request_id;
    dubbingLease.expires = Date.now() + 3000;
    clearTimeout(dubbingLease.timer);
    const leaseId = dubbingLease.lease_id;
    dubbingLease.timer = setTimeout(() => {
      if (dubbingLease?.lease_id === leaseId) restoreDubbing("expired", "lease_expired");
    }, 3000);
    dubbingReport(dubbingLease, "active");
  });

  function watchPlayback(video) {
    if (watchedVideo === video) return;
    if (watchedVideo) {
      for (const [kind, handler] of playbackHandlers) watchedVideo.removeEventListener?.(kind, handler);
    }
    playbackHandlers.clear();
    watchedVideo = video;
    waitingForPlayback = false;
    if (!video?.addEventListener) return;
    for (const kind of playbackEvents) {
      const handler = () => {
        if (kind === "seeking") seekSerial++;
        if (["waiting", "stalled", "emptied"].includes(kind)) waitingForPlayback = true;
        if (["playing", "loadedmetadata"].includes(kind)) waitingForPlayback = false;
        report(kind);
      };
      playbackHandlers.set(kind, handler);
      video.addEventListener(kind, handler);
    }
    const volumeHandler = () => {
      if (dubbingLease?.video === video && (video.muted !== true || Number(video.volume) !== dubbingLease.volume)) {
        restoreDubbing("user_override", "user_volume", true);
      }
    };
    playbackHandlers.set("volumechange", volumeHandler);
    video.addEventListener("volumechange", volumeHandler);
  }

  function report(playbackEvent = "") {
    let data = null;
    try {
      const identity = pageIdentity();
      const found = identity && currentVideo(identity);
      checkDubbing(found);
      watchPlayback(found?.video || null);
      if (found) {
        const {video, ...metadata} = found;
        data = {...metadata, position: Number.isFinite(video.currentTime) ? video.currentTime : 0,
          duration: Number.isFinite(video.duration) ? video.duration : 0,
          paused: video.paused || video.ended || video.readyState < 2, ended: video.ended === true,
          rate: video.playbackRate,
          seeking: Boolean(video.seeking), buffering: Boolean(waitingForPlayback || video.readyState < 3),
          seek_serial: seekSerial, dubbing_protocol: 1,
          playback_event: playbackEvents.includes(playbackEvent) ? playbackEvent : "",
          active: document.visibilityState === "visible"};
      }
      const currentKey = found ? nativeIdentity(found) : "";
      for (const request of captionRequests.values()) if (request.identity !== currentKey) request.controller.abort();
    } catch (_) { /* A SPA may temporarily remove the player. */ }
    window.postMessage({kind: MESSAGE, document_start: docStart, sequence: ++sequence,
      href: location.href, video: data}, location.origin);
  }
  window.addEventListener("pagehide", () => {
    restoreDubbing("restored", "page_closed");
    for (const request of captionRequests.values()) request.controller.abort();
    window.postMessage({kind: MESSAGE, document_start: docStart,
      sequence: ++sequence, href: location.href, video: null}, location.origin);
  });
  setInterval(report, 500);
  report();
})();
