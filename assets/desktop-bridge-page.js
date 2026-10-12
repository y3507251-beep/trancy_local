/* Passive MAIN-world observer. It never downloads media or starts recognition. */
(() => {
  "use strict";
  if (window !== window.top || window.__trancyDesktopObserver) return;
  window.__trancyDesktopObserver = true;
  const MESSAGE = "trancy-desktop-video-v1";
  const docStart = performance.timeOrigin;
  const initialHref = location.href;
  const biliCaptures = new Map();
  let sequence = 0;

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

  function inspectBiliResponse(requestUrl, payload, requestedPage) {
    try {
      const url = new URL(requestUrl, location.href);
      const current = pageIdentity();
      if (!current || current.platform !== "bilibili" || url.hostname !== "api.bilibili.com" ||
          !/^\/x\/player\/(?:wbi\/)?playurl$/.test(url.pathname) || payload?.code !== 0) return;
      if (!requestedPage || requestedPage.url !== current.url) return;
      const cid = url.searchParams.get("cid");
      if (!/^[1-9][0-9]{0,19}$/.test(cid || "")) return;
      const state = window.__INITIAL_STATE__?.videoData;
      const bvid = url.searchParams.get("bvid") || (String(state?.aid) === url.searchParams.get("avid") ? state?.bvid : "");
      if (bvid !== current.video_id) return;
      const pageInfo = state?.bvid === bvid ? state.pages?.find(p => Number(p.page) === current.page) : null;
      if (pageInfo && String(pageInfo.cid) !== cid) return;
      biliCaptures.set(`${bvid}:${current.page}`, {cid, audio: biliAudio(payload.data, cid)});
      while (biliCaptures.size > 8) biliCaptures.delete(biliCaptures.keys().next().value);
    } catch (_) { /* Never interfere with the site's request. */ }
  }

  // Observe only Bilibili playurl JSON. No credential or general request capture.
  if (location.hostname === "bilibili.com" || location.hostname.endsWith(".bilibili.com")) {
    const originalFetch = window.fetch;
    window.fetch = function (...args) {
      const requestUrl = typeof args[0] === "string" ? args[0] : args[0]?.url;
      const requestedPage = pageIdentity();
      return originalFetch.apply(this, args).then(response => {
        if (typeof requestUrl === "string" && requestUrl.includes("/playurl")) {
          response.clone().json().then(data => inspectBiliResponse(requestUrl, data, requestedPage)).catch(() => {});
        }
        return response;
      });
    };
    const originalOpen = XMLHttpRequest.prototype.open;
    const xhrRequests = new WeakMap();
    const observedRequests = new WeakSet();
    XMLHttpRequest.prototype.open = function (method, url, ...rest) {
      xhrRequests.set(this, {url: String(url), page: pageIdentity()});
      if (!observedRequests.has(this)) {
        observedRequests.add(this);
        this.addEventListener("load", () => {
          try {
            const request = xhrRequests.get(this);
            if (!request?.url.includes("/playurl")) return;
            const payload = this.responseType === "json" ? this.response : JSON.parse(this.responseText);
            inspectBiliResponse(request.url, payload, request.page);
          } catch (_) { /* Unrelated or unreadable response. */ }
        });
      }
      return originalOpen.call(this, method, url, ...rest);
    };
  }

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
    return {...identity, video, cid, title: state?.bvid === identity.video_id ? state.title : document.title,
      audio_urls: audio.slice(0, 12), ad_playing: false, is_live: false};
  }

  function report() {
    let data = null;
    try {
      const identity = pageIdentity();
      const found = identity && currentVideo(identity);
      if (found) {
        const {video, ...metadata} = found;
        data = {...metadata, position: Number.isFinite(video.currentTime) ? video.currentTime : 0,
          duration: Number.isFinite(video.duration) ? video.duration : 0,
          paused: video.paused || video.ended || video.readyState < 2, rate: video.playbackRate,
          active: document.visibilityState === "visible"};
      }
    } catch (_) { /* A SPA may temporarily remove the player. */ }
    window.postMessage({kind: MESSAGE, document_start: docStart, sequence: ++sequence,
      href: location.href, video: data}, location.origin);
  }
  window.addEventListener("pagehide", () => window.postMessage({kind: MESSAGE, document_start: docStart,
    sequence: ++sequence, href: location.href, video: null}, location.origin));
  setInterval(report, 500);
  report();
})();
