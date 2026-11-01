/**
 * 证据链：聊天回复里的引用来源。
 *
 * 后端在 assistant 消息的 `meta.citations`（历史）和流式 `done` 事件里
 * 都下发同一份 `{title, url, snippet}`；这里负责把不可信数据洗干净：
 * 只留 http/https、按 url 去重、最多 5 条、文本截断 200 字。
 */

export type Citation = {
  title: string;
  url: string;
  snippet?: string;
};

export const MAX_CITATIONS = 5;
export const MAX_CITATION_TEXT = 200;

/** 压成一行并截断：来源标题/摘要不允许多行撑破气泡。 */
function cleanText(value: unknown, limit: number): string {
  if (typeof value !== "string") return "";
  return value.replace(/\s+/g, " ").trim().slice(0, limit);
}

/**
 * 只放行 http/https 的绝对地址。
 * `javascript:`、`data:`、相对路径等一律拒绝，返回 null 由调用方丢弃。
 */
export function safeExternalUrl(raw: unknown): string | null {
  if (typeof raw !== "string") return null;
  const value = raw.trim();
  if (!value) return null;
  try {
    const parsed = new URL(value);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;
    return parsed.toString();
  } catch {
    return null;
  }
}

/** 把后端/历史里的原始引用数组洗成可渲染列表。 */
export function normalizeCitations(raw: unknown): Citation[] {
  if (!Array.isArray(raw)) return [];
  const out: Citation[] = [];
  const seen = new Set<string>();
  for (const item of raw) {
    if (!item || typeof item !== "object") continue;
    const record = item as Record<string, unknown>;
    const title = cleanText(record.title, MAX_CITATION_TEXT);
    const url = safeExternalUrl(record.url ?? record.href);
    if (!title || !url || seen.has(url)) continue;
    seen.add(url);
    const snippet = cleanText(
      record.snippet ?? record.body ?? record.content,
      MAX_CITATION_TEXT,
    );
    out.push(snippet ? { title, url, snippet } : { title, url });
    if (out.length >= MAX_CITATIONS) break;
  }
  return out;
}

/** 历史消息 meta 与流式事件都可能是「带 citations 字段的对象」。 */
function citationsField(raw: unknown): unknown {
  if (Array.isArray(raw)) return raw;
  if (raw && typeof raw === "object") {
    const value = (raw as Record<string, unknown>).citations;
    if (Array.isArray(value)) return value;
  }
  return [];
}

/** `/chat/history` 的 `meta` → 引用列表（兼容缺字段/旧数据）。 */
export function citationsFromMeta(meta: unknown): Citation[] {
  return normalizeCitations(citationsField(meta));
}

/** 流式 `done` 事件 → 引用列表（与 meta 共用同一条解析路径）。 */
export function citationsFromEvent(event: unknown): Citation[] {
  return normalizeCitations(citationsField(event));
}
