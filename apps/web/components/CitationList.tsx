"use client";

import { normalizeCitations, type Citation } from "@/lib/citations";

/** 引用面板：紧凑的「来源」列表，序号 + 标题 + 域名，新窗口打开。 */
export default function CitationList({
  citations,
  className = "",
}: {
  citations?: Citation[] | unknown;
  className?: string;
}) {
  const items = normalizeCitations(citations);
  if (!items.length) return null;

  return (
    <div className={className} aria-label="来源" data-testid="citation-list">
      <p className="mt-2 mb-1 text-[10px] tracking-[0.14em] uppercase" style={{ color: "var(--muted)" }}>
        来源
      </p>
      <ol className="flex flex-col gap-1">
        {items.map((c, i) => (
          <li key={c.url} className="flex items-start gap-1.5 text-[11px] leading-relaxed">
            <span className="w-3.5 shrink-0 text-center" style={{ color: "#6b6c7a" }} aria-hidden>
              {i + 1}
            </span>
            <a
              href={c.url}
              target="_blank"
              rel="noreferrer noopener"
              title={c.snippet || c.title}
              className="min-w-0 truncate transition-colors hover:text-indigo-300"
              style={{ color: "#b9bac8" }}
            >
              {c.title}
              <span className="ml-1.5 text-[10px]" style={{ color: "#6b6c7a" }}>
                {hostOf(c.url)}
              </span>
            </a>
          </li>
        ))}
      </ol>
    </div>
  );
}

function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return "";
  }
}
