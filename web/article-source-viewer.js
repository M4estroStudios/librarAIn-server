const SOURCE_HREF_RE = /^source:([a-f0-9]+):aligned:(\d+)$/i;

const pagePreviewOverlay = document.getElementById("page-preview-overlay");
const pagePreviewTitle = document.getElementById("page-preview-title");
const pagePreviewImg = document.getElementById("page-preview-img");
const pagePreviewPrev = document.getElementById("page-preview-prev");
const pagePreviewNext = document.getElementById("page-preview-next");
const pagePreviewBackdrop = document.getElementById("page-preview-backdrop");
const pagePreviewClose = document.getElementById("page-preview-close");

let pagePreviewState = { sha: "", pages: [], index: 0 };
const viewerPagesCache = new Map();

function parseSourceHref(href) {
  const match = String(href || "").trim().match(SOURCE_HREF_RE);
  if (!match) return null;
  return { sha: match[1].toLowerCase(), page: parseInt(match[2], 10) };
}

function pagePreviewUrl(sourceSha256, alignedPage) {
  const params = new URLSearchParams({
    source_sha256: sourceSha256,
    aligned_page: String(alignedPage),
  });
  return "/api/research/book-pages/render?" + params.toString();
}

async function loadViewerPages(sha) {
  if (viewerPagesCache.has(sha)) return viewerPagesCache.get(sha);
  const res = await fetch(
    "/api/research/books/meta?source_sha256=" + encodeURIComponent(sha)
  );
  const data = await res.json();
  const pages = data.ok && Array.isArray(data.viewer_pages) ? data.viewer_pages : [];
  viewerPagesCache.set(sha, pages);
  return pages;
}

function refreshPagePreview() {
  const aligned = pagePreviewState.pages[pagePreviewState.index];
  pagePreviewTitle.textContent = "Pagina " + aligned;
  pagePreviewImg.alt = "Pagina " + aligned;
  pagePreviewPrev.disabled = pagePreviewState.index <= 0;
  pagePreviewNext.disabled = pagePreviewState.index >= pagePreviewState.pages.length - 1;
  pagePreviewImg.src = pagePreviewUrl(pagePreviewState.sha, aligned);
}

function openPagePreview(sourceSha256, alignedPage, viewerPages) {
  let pages = Array.isArray(viewerPages) && viewerPages.length
    ? viewerPages.slice()
    : [alignedPage];
  let index = pages.indexOf(alignedPage);
  if (index < 0) {
    pages.push(alignedPage);
    pages.sort((a, b) => a - b);
    index = pages.indexOf(alignedPage);
  }
  pagePreviewState = { sha: sourceSha256, pages, index };
  pagePreviewOverlay.classList.remove("hidden");
  pagePreviewOverlay.setAttribute("aria-hidden", "false");
  document.body.style.overflow = "hidden";
  refreshPagePreview();
}

function closePagePreview() {
  pagePreviewOverlay.classList.add("hidden");
  pagePreviewOverlay.setAttribute("aria-hidden", "true");
  document.body.style.overflow = "";
  pagePreviewImg.removeAttribute("src");
}

async function onSourceLinkClick(event) {
  const anchor = event.target.closest('a[href^="source:"]');
  if (!anchor) return;
  const parsed = parseSourceHref(anchor.getAttribute("href"));
  if (!parsed || parsed.page < 1) return;
  event.preventDefault();
  const viewerPages = await loadViewerPages(parsed.sha);
  openPagePreview(parsed.sha, parsed.page, viewerPages);
}

document.addEventListener("click", onSourceLinkClick);

pagePreviewPrev.addEventListener("click", () => {
  if (pagePreviewState.index <= 0) return;
  pagePreviewState.index -= 1;
  refreshPagePreview();
});

pagePreviewNext.addEventListener("click", () => {
  if (pagePreviewState.index >= pagePreviewState.pages.length - 1) return;
  pagePreviewState.index += 1;
  refreshPagePreview();
});

pagePreviewClose.addEventListener("click", closePagePreview);
pagePreviewBackdrop.addEventListener("click", closePagePreview);
pagePreviewOverlay.querySelector(".page-preview-dialog").addEventListener("click", (event) => {
  event.stopPropagation();
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !pagePreviewOverlay.classList.contains("hidden")) {
    closePagePreview();
  }
});
