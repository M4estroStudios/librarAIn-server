import { apiFetch, preflightOrBlock, articleUrl } from "./api.js";
import { renderPohCard } from "./poh-card.js";

const messages = [];
let chatBox;
let citedPohs = new Map();
let firstMessage = true;
let showToolCalls = false;
let showThinking = false;

export function initPerplexitySearch() {
  chatBox = document.getElementById("chat-messages");
  const form = document.getElementById("chat-form");
  const input = document.getElementById("chat-input");
  const toolToggle = document.getElementById("show-tool-calls");
  const thinkingToggle = document.getElementById("show-thinking");
  if (toolToggle) {
    toolToggle.addEventListener("change", () => {
      showToolCalls = toolToggle.checked;
      syncDebugVisibility();
    });
  }
  if (thinkingToggle) {
    thinkingToggle.addEventListener("change", () => {
      showThinking = thinkingToggle.checked;
      syncDebugVisibility();
    });
  }
  if (form && input) {
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const text = (input.value || "").trim();
      if (!text) return;
      input.value = "";
      sendMessage(text);
    });
  }
  const cta = document.getElementById("perplexity-cta");
  if (cta) {
    cta.addEventListener("click", () => {
      const lines = [messages.find((m) => m.role === "user")?.content || ""];
      citedPohs.forEach((p) => {
        lines.push(`- ${p.label} (${p.poh_id}): ${(p.books || []).map((b) => b.title).join(", ")}`);
      });
      window.__librarainDashboard.openIngestWithContext(lines.join("\n"));
    });
  }
}

function appendMsg(role, html) {
  if (!chatBox) return;
  const div = document.createElement("div");
  div.className = `chat-msg ${role}`;
  div.innerHTML = html;
  chatBox.appendChild(div);
  chatBox.scrollTop = chatBox.scrollHeight;
}

function createAssistantBlock() {
  const root = document.createElement("div");
  root.className = "chat-msg assistant";
  const thinkingPanel = document.createElement("details");
  thinkingPanel.className = "chat-debug-panel chat-thinking-panel hidden";
  thinkingPanel.innerHTML = "<summary>Thinking</summary><pre class=\"chat-debug-body\"></pre>";
  const toolsPanel = document.createElement("details");
  toolsPanel.className = "chat-debug-panel chat-tools-panel hidden";
  toolsPanel.innerHTML = "<summary>Tool calls</summary><div class=\"chat-debug-body chat-tools-list\"></div>";
  const answerEl = document.createElement("div");
  answerEl.className = "chat-answer";
  answerEl.textContent = "…";
  root.appendChild(thinkingPanel);
  root.appendChild(toolsPanel);
  root.appendChild(answerEl);
  chatBox.appendChild(root);
  chatBox.scrollTop = chatBox.scrollHeight;
  return {
    root,
    thinkingPanel,
    thinkingBody: thinkingPanel.querySelector(".chat-debug-body"),
    toolsPanel,
    toolsList: toolsPanel.querySelector(".chat-tools-list"),
    answerEl,
    thinkingText: "",
    toolItems: new Map(),
  };
}

function syncDebugVisibility() {
  if (!chatBox) return;
  chatBox.querySelectorAll(".chat-thinking-panel").forEach((el) => {
    const hasContent = Boolean(el.dataset.hasContent);
    el.classList.toggle("hidden", !showThinking || !hasContent);
  });
  chatBox.querySelectorAll(".chat-tools-panel").forEach((el) => {
    const hasContent = Boolean(el.dataset.hasContent);
    el.classList.toggle("hidden", !showToolCalls || !hasContent);
  });
}

function appendThinking(block, text) {
  if (!text) return;
  block.thinkingText += text;
  block.thinkingBody.textContent = block.thinkingText;
  block.thinkingPanel.dataset.hasContent = "1";
  block.thinkingPanel.open = showThinking;
  syncDebugVisibility();
}

function appendToolEvent(block, event) {
  if (event.status === "start") {
    const item = document.createElement("div");
    item.className = "chat-tool-item";
    item.dataset.toolKey = `${event.round || 0}:${event.name}:${block.toolItems.size}`;
    item.innerHTML =
      `<div class="chat-tool-name">${escape(event.name || "tool")}</div>` +
      `<pre class="chat-tool-args">${escape(formatJson(event.arguments))}</pre>` +
      `<div class="chat-tool-result hint">In esecuzione…</div>`;
    block.toolsList.appendChild(item);
    block.toolItems.set(item.dataset.toolKey, item);
    block.toolsPanel.dataset.hasContent = "1";
    block.toolsPanel.open = showToolCalls;
    syncDebugVisibility();
    return;
  }
  if (event.status !== "result") return;
  const keys = [...block.toolItems.keys()].filter((k) => k.includes(`:${event.name}:`));
  const key = keys[keys.length - 1];
  const item = key ? block.toolItems.get(key) : null;
  if (!item) return;
  const resultEl = item.querySelector(".chat-tool-result");
  if (resultEl) {
    resultEl.className = "chat-tool-result";
    resultEl.innerHTML = `<pre>${escape(formatJson(event.result))}</pre>`;
  }
  if (event.name === "offerArticleGeneration") {
    try {
      const parsed = JSON.parse(event.result || "{}");
      if (parsed.poh_id) {
        block.toolsList.appendChild(renderPohCard({ poh_id: parsed.poh_id, label: parsed.label || parsed.poh_id, has_article: false }));
      }
    } catch {}
  }
}

function formatJson(raw) {
  if (raw == null || raw === "") return "";
  if (typeof raw === "object") {
    try {
      return JSON.stringify(raw, null, 2);
    } catch {
      return String(raw);
    }
  }
  const text = String(raw);
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

function linkifyCitations(text) {
  return text.replace(/\[([^\]]+)\]\(poh:([^)]+)\)/gi, (_, label, id) => {
    citedPohs.set(id, { poh_id: id, label });
    return `<a class="citation" href="${articleUrl(id)}" target="_blank">${label}</a>`;
  }).replace(/(poh:[\w.\-]+)/gi, (m) => {
    const id = m.replace(/^poh:/, "");
    citedPohs.set(id, { poh_id: id, label: id });
    return `<a class="citation" href="${articleUrl(id)}" target="_blank">${id}</a>`;
  });
}

export async function sendMessage(text) {
  if (firstMessage) {
    if (!(await preflightOrBlock("research"))) return;
    firstMessage = false;
  }
  messages.push({ role: "user", content: text });
  appendMsg("user", escape(text));
  const block = createAssistantBlock();
  try {
    const res = await apiFetch("/api/chat/completions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        messages: messages.slice(-20),
        stream: true,
      }),
    });
    if (!res.ok) {
      const err = await res.text();
      block.answerEl.textContent = `Errore: ${err}`;
      return;
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let full = "";
    block.answerEl.textContent = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      const chunk = decoder.decode(value, { stream: true });
      chunk.split("\n").forEach((line) => {
        if (!line.startsWith("data: ")) return;
        const payload = line.slice(6).trim();
        if (payload === "[DONE]") return;
        try {
          const json = JSON.parse(payload);
          const meta = json.librarain;
          if (meta) {
            if (meta.type === "thinking") appendThinking(block, meta.content || "");
            if (meta.type === "tool_call") appendToolEvent(block, meta);
            if (meta.type === "error") block.answerEl.textContent = `Errore: ${meta.message || "sconosciuto"}`;
            return;
          }
          const delta = json.choices?.[0]?.delta?.content || "";
          if (delta) {
            full += delta;
            block.answerEl.innerHTML = linkifyCitations(escape(full));
          }
        } catch {}
      });
    }
    if (!full && !block.thinkingText && !block.toolsPanel.dataset.hasContent) {
      block.answerEl.textContent = "(nessuna risposta)";
    }
    messages.push({ role: "assistant", content: full });
    scanOfferGeneration(full, block);
  } catch (err) {
    block.answerEl.textContent = String(err.message || err);
  }
}

function scanOfferGeneration(text, block) {
  const re = /offerArticleGeneration\s*\(\s*['"]?([\w.\-]+)/i;
  const m = text.match(re);
  if (m) block.toolsList.appendChild(renderPohCard({ poh_id: m[1], label: m[1], has_article: false }));
}

function escape(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;");
}
