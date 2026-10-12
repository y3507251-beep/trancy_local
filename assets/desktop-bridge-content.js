/* Isolated-world relay. Website messages cannot invoke translation or downloads. */
(() => {
  "use strict";
  if (window !== window.top) return;
  let alive = true;
  let lastSequence = 0;
  window.addEventListener("message", event => {
    if (!alive || event.source !== window || event.origin !== location.origin ||
        event.data?.kind !== "trancy-desktop-video-v1") return;
    const data = event.data;
    if (data.document_start !== performance.timeOrigin || !Number.isSafeInteger(data.sequence) ||
        data.sequence <= lastSequence || data.href !== location.href) return;
    lastSequence = data.sequence;
    const video = data.video;
    if (video !== null && (typeof video !== "object" || !["bilibili", "youtube"].includes(video.platform))) return;
    // Existing Trancy background listeners expect a `to` array on all messages.
    try {
      chrome.runtime.sendMessage({to: [], kind: "trancy-desktop-report-v1", href: location.href,
        document_start: data.document_start, sequence: data.sequence, video}).catch(() => { alive = false; });
    } catch (_) { alive = false; }
  });
})();
