/* Isolated-world relay. Website messages cannot invoke translation or downloads. */
(() => {
  "use strict";
  if (window !== window.top) return;
  let alive = true;
  let lastSequence = 0;
  const pendingCaptions = new Set();
  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
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
        !["trancy-desktop-video-v1", "trancy-desktop-captions-v1"].includes(event.data?.kind)) return;
    const data = event.data;
    if (data.document_start !== performance.timeOrigin || !Number.isSafeInteger(data.sequence) ||
        data.sequence <= lastSequence || data.href !== location.href) return;
    lastSequence = data.sequence;
    const video = data.video;
    if (video !== null && (typeof video !== "object" || !["bilibili", "youtube"].includes(video.platform))) return;
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
