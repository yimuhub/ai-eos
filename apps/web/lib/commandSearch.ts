// ⌘K 命令面板的纯检索函数（可单测、不依赖 React / DOM）。
//
// ## 拼音从哪来
// 项目里没有运行时的"汉字 → 拼音"转换表，拼音检索一直是**预生成别名**：
// `lib/nav.ts` 的 `keywords` 里已经写了 `renwu` / `gongzuotai` / `moxing` 等，
// 命令面板直接把这些别名当普通文本参与打分即可。
// 这里额外支持"拼音首字母"式的**子序列**匹配（`zhzx` → `zhihuizhongxin`），
// 复用的仍是同一份别名数据，没有另造一套拼音库。
//
// 打分绝对值无意义，只用于稳定排序；0 表示不匹配。

const DESC_WEIGHT = 0.5;
const KEYWORD_WEIGHT = 0.9;
const GROUP_WEIGHT = 0.25;
const HREF_WEIGHT = 0.4;

/** 大小写、全半角、多余空白都不应影响检索。 */
function normalize(text: string): string {
  return text
    .normalize("NFKC")
    .toLocaleLowerCase()
    .replace(/\s+/g, " ")
    .trim();
}

/** 去掉空白与常见分隔符，"ren wu" 也能命中 "renwu"。 */
function compact(text: string): string {
  return text.replace(/[\s\-_/.:]+/g, "");
}

/** 判断 needle 是否是 haystack 的子序列（用于拼音首字母，如 zhzx → zhihuizhongxin）。 */
function isSubsequence(needle: string, haystack: string): boolean {
  if (!needle || needle.length < 2) return false;
  let i = 0;
  for (const ch of haystack) {
    if (ch === needle[i]) i += 1;
    if (i === needle.length) return true;
  }
  return false;
}

/**
 * 给一段文本打分。空查询返回 0；不匹配返回 0。
 *
 * 命中优先级：完全相等 > 前缀 > 词首 > 子串 > 去分隔符子串 > 拉丁子序列。
 * 中文子串、英文大小写不敏感都由 normalize + includes 覆盖。
 */
export function scoreCommand(query: string, text: string): number {
  const q = normalize(query);
  if (!q) return 0;
  const t = normalize(text);
  if (!t) return 0;

  if (t === q) return 120;
  if (t.startsWith(q)) return 100;

  const tokens = t.split(/[\s\-_/.:]+/).filter(Boolean);
  if (tokens.some((token) => token === q)) return 96;
  if (tokens.some((token) => token.startsWith(q))) return 88;

  if (t.includes(q)) return 76;

  // "ren wu" 与 "renwu"、"token stats" 与 "tokenstats" 视为同一类命中
  const compactQ = compact(q);
  const compactT = compact(t);
  if (compactQ && compactT.includes(compactQ)) return 60;

  // 拼音首字母：查询必须全是拉丁字母，避免中文查询误触
  if (/^[a-z]+$/.test(q)) {
    const latin = t.replace(/[^a-z0-9]/g, "");
    if (isSubsequence(q, latin)) return 34;
  }

  return 0;
}

export type SearchableCommand = {
  id: string;
  label: string;
  desc?: string;
  /** 检索别名：英文、拼音、口语说法。拼音检索完全复用这份既有数据。 */
  keywords?: string[];
  group?: string;
  href?: string;
};

/** 一条命令的综合得分：标题权重最高，别名次之，说明/分组/路径只做兜底。 */
function scoreEntry(query: string, command: SearchableCommand): number {
  const scores = [scoreCommand(query, command.label)];
  const keywordScores = (command.keywords || []).map((kw) => scoreCommand(query, kw));
  scores.push(Math.round(Math.max(0, ...keywordScores) * KEYWORD_WEIGHT));
  if (command.desc) scores.push(Math.round(scoreCommand(query, command.desc) * DESC_WEIGHT));
  if (command.group) scores.push(Math.round(scoreCommand(query, command.group) * GROUP_WEIGHT));
  if (command.href) scores.push(Math.round(scoreCommand(query, command.href) * HREF_WEIGHT));
  return Math.max(0, ...scores);
}

/**
 * 过滤并排序命令。
 *
 * - 空查询：按原顺序返回（仍去重、仍受 limit 限制），让"最近访问 / 分组"由调用方决定；
 * - 非空查询：分数降序，同分保持输入顺序（显式用 order 兜底，不依赖引擎排序稳定性）；
 * - 按 id 去重，保留第一次出现的项。
 */
export function filterCommands<T extends SearchableCommand>(
  commands: T[],
  query: string,
  limit = 50,
): T[] {
  const cap = Number.isFinite(limit) ? Math.max(0, Math.floor(limit)) : Number.POSITIVE_INFINITY;
  const seen = new Set<string>();
  const unique: T[] = [];
  for (const command of commands) {
    if (!command || typeof command.id !== "string" || seen.has(command.id)) continue;
    seen.add(command.id);
    unique.push(command);
  }

  const q = normalize(query);
  if (!q) return unique.slice(0, cap);

  const scored = unique
    .map((command, order) => ({ command, order, score: scoreEntry(q, command) }))
    .filter((item) => item.score > 0)
    .sort((a, b) => b.score - a.score || a.order - b.order);

  return scored.slice(0, cap).map((item) => item.command);
}

/** 循环索引：0 → 末尾、末尾 → 0，空列表恒为 0。 */
export function nextIndex(current: number, length: number, delta: number): number {
  const len = Number.isFinite(length) ? Math.floor(length) : 0;
  if (len <= 0) return 0;
  const cur = Number.isFinite(current) ? Math.trunc(current) : 0;
  const step = Number.isFinite(delta) ? Math.trunc(delta) : 0;
  return ((cur + step) % len + len) % len;
}
