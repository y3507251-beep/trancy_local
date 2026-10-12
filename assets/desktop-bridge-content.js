/* Isolated-world relay. Website messages cannot invoke translation or downloads. */
(() => {
  "use strict";
  if (window !== window.top) return;
  let alive = true;
  let lastSequence = 0;
  const pendingCaptions = new Set();
  const pendingDubbing = new Map();
  const dubbingLeases = new Map();
  const dubbingStatuses = new Set(["active", "restored", "user_override", "expired", "video_changed", "error"]);
  const dubbingReasons = new Set(["", "disabled", "bridge_disconnected", "lease_expired", "video_changed", "ad_playing", "retired_lease", "replaced", "user_volume", "page_closed"]);
  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (alive && sender.id === chrome.runtime.id && message?.kind === "trancy-desktop-dubbing-request-v1") {
      if (message.document_start !== performance.timeOrigin || !/^[a-f0-9]{32}$/.test(message.request_id || "") ||
          !/^[a-f0-9]{32}$/.test(message.lease_id || "") || typeof message.enabled !== "boolean" ||
          message.video?.platform !== "bilibili" || pendingDubbing.has(message.request_id)) return;
      pendingDubbing.set(message.request_id, message.lease_id);
      dubbingLeases.set(message.lease_id, {video: message.video, document_start: message.document_start});
      window.postMessage({kind: "trancy-desktop-dubbing-command-v1", document_start: performance.timeOrigin,
        request_id: message.request_id, lease_id: message.lease_id, video: message.video, enabled: message.enabled,
        reason: message.reason === "bridge_disconnected" ? "bridge_disconnected" : ""}, location.origin);
      sendResponse({accepted: true});
      setTimeout(() => pendingDubbing.delete(message.request_id), 5000);
      return;
    }
    if (!alive || sender.id !== chrome.runtime.id || message?.kind !== "trancy-desktop-captions-request-v1" ||
        message.document_start !== performance.timeOrigin || !/^[a-f0-9]{32}$/.test(message.request_id || "") ||
        pendingCaptions.has(message.request_id)) return;
    pendingCaptions.add(message.request_id);
    window.postMessage({kind: "trancy-desktop-captions-command-v1", document_start: performance.timeOrigin,
      request_id: message.request_id, video: message.video, preferred_language: message.preferred_language}, location.origin);
    sendResponse({accepted: true});
    // Discard only request bookkeeping. Existing website subtitle caches stay intact.
    setTimeout(() => pendingCaptions.delete(message.request_id), 15000);
  });
  window.addEventListener("message", event => {
    if (!alive || event.source !== window || event.origin !== location.origin ||
        !["trancy-desktop-video-v1", "trancy-desktop-captions-v1", "trancy-desktop-dubbing-v1"].includes(event.data?.kind)) return;
    const data = event.data;
    if (data.document_start !== performance.timeOrigin || !Number.isSafeInteger(data.sequence) ||
        data.sequence <= lastSequence || data.href !== location.href) return;
    lastSequence = data.sequence;
    const video = data.video;
    if (video !== null && (typeof video !== "object" || !["bilibili", "youtube"].includes(video.platform))) return;
    if (data.kind === "trancy-desktop-dubbing-v1") {
      const lease = dubbingLeases.get(data.lease_id);
      if (!lease || video?.platform !== "bilibili" || !dubbingStatuses.has(data.status) ||
          !/^[a-f0-9]{32}$/.test(data.request_id || "") || video.video_id !== lease.video.video_id ||
          video.url !== lease.video.url || Number(video.page) !== Number(lease.video.page) ||
          String(video.cid) !== String(lease.video.cid) ||
          (data.status === "active" && pendingDubbing.get(data.request_id) !== data.lease_id)) return;
      pendingDubbing.delete(data.request_id);
      if (data.status !== "active") dubbingLeases.delete(data.lease_id);
      try {
        chrome.runtime.sendMessage({to: [], kind: "trancy-desktop-dubbing-report-v1", href: location.href,
          document_start: data.document_start, sequence: data.sequence, request_id: data.request_id,
          lease_id: data.lease_id, video: lease.video, status: data.status,
          reason: dubbingReasons.has(data.reason) ? data.reason : "",
          position: Number(data.position) || 0, paused: Boolean(data.paused), rate: Number(data.rate) || 1,
          seeking: Boolean(data.seeking), buffering: Boolean(data.buffering),
          seek_serial: Number.isSafeInteger(data.seek_serial) && data.seek_serial >= 0 ? data.seek_serial : 0}).catch(() => { alive = false; });
      } catch (_) { alive = false; }
      return;
    }
    if (data.kind === "trancy-desktop-captions-v1") {
      if (!pendingCaptions.has(data.request_id)) return;
      pendingCaptions.delete(data.request_id);
      try {
        chrome.runtime.sendMessage({to: [], kind: "trancy-desktop-captions-report-v1", href: location.href,
          document_start: data.document_start, sequence: data.sequence, request_id: data.request_id,
          video, status: data.status, source_language: data.source_language, caption_source: data.caption_source,
          error_code: data.error_code, native_track_available: data.native_track_available,
          cues: data.cues}).catch(() => { alive = false; });
      } catch (_) { alive = false; }
      return;
    }
    // Existing Trancy background listeners expect a `to` array on all messages.
    try {
      chrome.runtime.sendMessage({to: [], kind: "trancy-desktop-report-v1", href: location.href,
        document_start: data.document_start, sequence: data.sequence, video}).catch(() => { alive = false; });
    } catch (_) { alive = false; }
  });
})();
