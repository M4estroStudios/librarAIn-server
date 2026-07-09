export function initEmbedFrameListener(frameId, source) {
  const frame = document.getElementById(frameId);
  if (!frame) return;
  window.addEventListener("message", (event) => {
    const data = event.data;
    if (!data || data.type !== "librarain-embed-height" || data.source !== source) return;
    const height = Math.max(320, Number(data.height) || 0);
    frame.style.height = `${height}px`;
    frame.style.minHeight = "0";
  });
}

export function initAdmin() {
  initEmbedFrameListener("admin-frame", "admin");
  const frame = document.getElementById("admin-frame");
  if (frame) {
    window.addEventListener("message", (event) => {
      if (event.data && event.data.type === "librarain-jobs-refresh") {
        frame.contentWindow?.postMessage({ type: "librarain-jobs-refresh" }, "*");
      }
    });
  }
}
