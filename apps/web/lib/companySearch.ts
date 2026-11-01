// B5「公司大脑」前端检索层：封装 GET /search 与结果映射。
//
// 后端返回的 snippet 可能较长（带上下文），命令面板一行展示，统一截到 80 字；
// 分组顺序由后端决定（任务 → 记忆 → 知识库 → 资产），这里只做**保持**，
// 不重排。重复项（同一 type + id）只保留第一次出现，避免面板里出现两行同物。
//
// 本文件只做"请求 + 纯映射"，不依赖 React / DOM，纯函数可直接单测。

import { api } from "./api";

export const MIN_COMPANY_QUERY_LENGTH = 2;
export const SNIPPET_MAX = 80;
export const COMPANY_SEARCH_TYPES = [
  "task",
  "memory",
  "knowledge",
  "asset",
] as const;

export type CompanySearchType = (typeof COMPANY_SEARCH_TYPES)[number];

export type CompanySearchItem = {
  id: string;
  title: string;
  snippet: string;
  href: string;
};

export type CompanySearchGroup = {
  type: string;
  label: string;
  items: CompanySearchItem[];
};

const TYPE_LABELS: Record<string, string> = {
  task: "任务",
  memory: "记忆",
  knowledge: "知识库",
  asset: "资产",
};

function asText(value: unknown): string {
  return typeof value === "string" ? value : "";
}

/** 折叠空白并截到 max 个字符（含省略号本身）；按 code point 处理中文与 emoji。 */
export function truncateSnippet(value: string, max = SNIPPET_MAX): string {
  const flat = (value || "").replace(/\s+/g, " ").trim();
  if (max <= 0) return "";
  const chars = Array.from(flat);
  if (chars.length <= max) return flat;
  return chars.slice(0, max - 1).join("") + "…";
}

/**
 * 把后端响应映射成面板可渲染的分组。
 *
 * - 坏数据（非对象 / 缺 id / 缺 href）直接丢弃，不让一条脏数据打断整个面板；
 * - 空分组不输出；
 * - 顺序严格保持后端返回的分组顺序与组内顺序。
 */
export function normalizeCompanySearch(raw: unknown): CompanySearchGroup[] {
  if (!raw || typeof raw !== "object") return [];
  const groupsRaw = (raw as { groups?: unknown }).groups;
  if (!Array.isArray(groupsRaw)) return [];

  const seen = new Set<string>();
  const groups: CompanySearchGroup[] = [];

  for (const groupRaw of groupsRaw) {
    if (!groupRaw || typeof groupRaw !== "object") continue;
    const type = asText((groupRaw as { type?: unknown }).type);
    if (!type) continue;
    const itemsRaw = (groupRaw as { items?: unknown }).items;
    if (!Array.isArray(itemsRaw)) continue;

    const items: CompanySearchItem[] = [];
    for (const itemRaw of itemsRaw) {
      if (!itemRaw || typeof itemRaw !== "object") continue;
      const record = itemRaw as Record<string, unknown>;
      const id = asText(record.id);
      const title = asText(record.title);
      const href = asText(record.href);
      if (!id || !title || !href) continue;
      const key = `${type}:${id}`;
      if (seen.has(key)) continue;
      seen.add(key);
      items.push({
        id,
        title,
        snippet: truncateSnippet(asText(record.snippet)),
        href,
      });
    }

    if (!items.length) continue;
    const label = asText((groupRaw as { label?: unknown }).label)
      || TYPE_LABELS[type]
      || type;
    groups.push({ type, label, items });
  }

  return groups;
}

/** 构造请求路径（独立导出便于单测编码细节）。 */
export function buildCompanySearchPath(
  companyId: string,
  query: string,
  types?: readonly CompanySearchType[],
): string {
  const params = new URLSearchParams({ company_id: companyId, q: query });
  if (types && types.length) params.set("types", types.join(","));
  return `/search?${params.toString()}`;
}

export type CompanySearchRequest = {
  companyId: string;
  query: string;
  types?: readonly CompanySearchType[];
  signal?: AbortSignal;
};

/**
 * 请求公司资料检索；失败直接抛错，由调用方决定是否降级（面板只提示、不打断本地结果）。
 * `silent: true` 关闭全局错误 Toast——用户每敲一个字都弹一次「请求失败」是骚扰。
 */
export async function searchCompany(
  request: CompanySearchRequest,
): Promise<CompanySearchGroup[]> {
  const { companyId, query, types, signal } = request;
  if (!companyId || !query.trim()) return [];
  const typeQuery = types && types.length
    ? `&types=${encodeURIComponent(types.join(","))}`
    : "";
  const raw = await api.get<unknown>(
    `/search?company_id=${encodeURIComponent(companyId)}&q=${encodeURIComponent(query.trim())}${typeQuery}`,
    { signal, silent: true },
  );
  return normalizeCompanySearch(raw);
}
