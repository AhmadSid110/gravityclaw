import React from "react";
import { marked } from "marked";

// Custom renderer for code blocks
const renderer = new marked.Renderer();
renderer.code = function ({ text, lang }: { text: string; lang?: string }) {
  const language = (lang || "").trim() || "code";
  const escaped = (text || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
  const encoded = encodeURIComponent(text || "");
  return `<div class="md-code-block"><div class="md-code-header"><span class="md-code-lang">${language}</span><button type="button" class="md-code-copy-btn" data-code="${encoded}">Copy</button></div><pre class="md-code-content"><code>${escaped}</code></pre></div>`;
};

marked.setOptions({
  renderer,
  gfm: true,
  breaks: true,
});

export function MarkdownMessage({
  content,
  streaming = false,
}: {
  content: string;
  streaming?: boolean;
}) {
  if (!content) return null;

  let html = "";
  try {
    const raw = String(content);
    html = marked.parse(raw, { async: false }) as string;
  } catch {
    const safe = String(content)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
    html = `<p>${safe}</p>`;
  }

  const handleClick = (e: React.MouseEvent<HTMLDivElement>) => {
    const target = e.target as HTMLElement;
    if (target && target.classList.contains("md-code-copy-btn")) {
      const code = decodeURIComponent(target.getAttribute("data-code") || "");
      if (code) {
        void navigator.clipboard.writeText(code);
        target.textContent = "✓ Copied";
        target.classList.add("copied");
        setTimeout(() => {
          target.textContent = "Copy";
          target.classList.remove("copied");
        }, 2000);
      }
    }
  };

  return (
    <div
      className={`markdown-body ${streaming ? "is-streaming" : ""}`}
      onClick={handleClick}
    >
      <div dangerouslySetInnerHTML={{ __html: html }} />
      {streaming && <span className="streaming-cursor">▋</span>}
    </div>
  );
}
