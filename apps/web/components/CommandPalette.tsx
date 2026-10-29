"use client";

// ⌘K 全局命令面板（导航收敛的真正解法）
//
// ## 为什么不是"把菜单重新分组"
// 系统有 34 个可用页面 + 若干详情页，任何分组方案都只是把"找不到"推迟到第二层。
// 分组能改善**浏览**，但用户在真实场景里是**带着目标来**的——他心里想的是
// "我要配模型""我要看花了多少钱"，而不是"这应该属于哪一组"。
// 让用户按自己的词直接跳到目标，才是 30+ 页面规模下真正省事的方案。
//
// 侧边栏保留分组是为了"逛"，命令面板是为了"到"。
//
// ## 能力
//   - ⌘K / Ctrl+K 唤起（再按一次关闭）
//   - 检索范围：页面标题、说明、英文别名、拼音、口语说法、路径、所属分组
//   - 空查询时展示「最近访问」+ 全部页面，顺带让用户知道系统里有什么
//   - 严格键盘操作：↑↓ 选择、↵ 打开、Esc 关闭，全程不用碰鼠标
//
// 页面清单来自 `lib/nav.ts`——新增页面只改那一处，这里自动生效。

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { ALL_PAGES, type NavItem } from "@/lib/nav";
import { clearToken } from "@/lib/api";
import { notify } from "@/components/Toast";
import { openApp } from "@/lib/apps";

const RECENT_KEY = "aieos_recent_pages";
const RECENT_MAX = 6;

/** 读取最近访问的页面路径（工作台打开标签、命令面板跳转时写入） */
export function readRecentPages(): string[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = localStorage.getItem(RECENT_KEY);
    const arr = raw ? JSON.parse(raw) : [];
    return Array.isArray(arr) ? arr.filter((x) => typeof x === "string") : [];
  } catch {
    return [];
  }
}

/** 记录一次访问。幂等、去重、只留最近 N 条。 */
export function pushRecentPage(href: string) {
  if (typeof window === "undefined") return;
  try {
    const list = readRecentPages().filter((h) => h !== href);
    list.unshift(href);
    localStorage.setItem(RECENT_KEY, JSON.stringify(list.slice(0, RECENT_MAX)));
  } catch {
    /* 隐私模式下写不了 localStorage：最近访问失效，但不影响跳转本身 */
  }
}

type Result =
  | { kind: "page"; id: string; label: string; desc: string; icon: string; group: string; href: string }
  | { kind: "action"; id: string; label: string; desc: string; icon: string; run: () => void };

/** 打分：0 表示不匹配。数字只用于排序，绝对值无意义。 */
function scoreOf(q: string, page: NavItem & { group: string }): number {
  const label = page.label.toLowerCase();
  const href = page.href.toLowerCase();
  const desc = page.desc.toLowerCase();
  const group = page.group.toLowerCase();
  let best = 0;

  if (label === q) best = 120;
  else if (label.startsWith(q)) best = 100;
  else if (label.includes(q)) best = 80;

  for (const kw of page.keywords || []) {
    const k = kw.toLowerCase();
    if (k === q) best = Math.max(best, 92); // 用户按别名精确命中，意图很明确
    else if (k.startsWith(q)) best = Math.max(best, 70);
    else if (k.includes(q)) best = Math.max(best, 55);
  }

  if (desc.includes(q)) best = Math.max(best, 40);
  if (href.includes(q)) best = Math.max(best, 30);
  if (group.includes(q)) best = Math.max(best, 20);
  return best;
}

/** 打开命令面板。让侧边栏的搜索框、快捷键提示等入口不必各自管理弹窗状态。 */
let opener: (() => void) | null = null;
export function openCommandPalette() {
  opener?.();
}

export default function CommandPalette() {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [cursor, setCursor] = useState(0);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);

  // 暴露给外部入口（侧边栏搜索框）使用
  useEffect(() => {
    opener = () => setOpen(true);
    return () => {
      opener = null;
    };
  }, []);

  const actions = useMemo<Result[]>(
    () => [
      {
        kind: "action",
        id: "action:logout",
        label: "退出登录",
        desc: "清除本地凭据并返回登录页",
        icon: "⇤",
        run: async () => {
          try {
            await clearToken();
          } catch (e) {
            notify("warning", `退出时清理本地凭据失败：${(e as Error).message || e}`);
          }
          router.push("/login");
        },
      },
      {
        kind: "action",
        id: "action:clear-recent",
        label: "清空「最近访问」记录",
        desc: "只清除本机的访问历史，不影响任何数据",
        icon: "🧹",
        run: () => {
          try {
            localStorage.removeItem(RECENT_KEY);
          } catch {
            /* 存储不可写时无副作用 */
          }
          notify("success", "已清空最近访问记录");
        },
      },
    ],
    [router],
  );

  // ---------- 结果计算 ----------
  const results = useMemo<Result[]>(() => {
    const query = q.trim().toLowerCase();

    // "/" 只是跳转到工作台的别名，不能当成可打开的页面（会套娃）
    const reachable = ALL_PAGES.filter((p) => p.href !== "/");

    const pageHit = (p: (typeof ALL_PAGES)[number]): Result => ({
      kind: "page",
      id: `page:${p.href}`,
      label: p.label,
      desc: p.desc,
      icon: p.icon,
      group: p.group,
      href: p.href,
    });

    if (!query) {
      // 空查询：最近访问（真实使用习惯）+ 全部动作。
      // 这里刻意读一次 `open`：面板关闭时不需要算最近访问，而重新打开时必须重算，
      // 否则第二次打开会显示上一次的过期列表（查询串没变 → memo 不会重跑）。
      const recent = (open ? readRecentPages() : [])
        .map((href) => reachable.find((p) => p.href === href))
        .filter((p): p is (typeof ALL_PAGES)[number] => !!p)
        .map(pageHit);
      return [...recent, ...actions];
    }

    const scored = reachable.map((p) => ({ p, s: scoreOf(query, p) }))
      .filter((x) => x.s > 0)
      .sort((a, b) => b.s - a.s);
    const pages = scored.map((x) => pageHit(x.p));

    const act = actions.filter(
      (a) => a.label.toLowerCase().includes(query) || a.desc.toLowerCase().includes(query),
    );
    return [...pages, ...act];
  }, [q, actions, open]);

  // 查询变化后游标归零，否则会停在一个不存在的位置
  useEffect(() => {
    setCursor(0);
  }, [q]);

  const close = useCallback(() => {
    setOpen(false);
    setQ("");
  }, []);

  const go = useCallback(
    (r: Result | undefined) => {
      if (!r) return;
      close();
      // 页面不再整页跳走：工作台会把它开成右栏标签；没有工作台时才回退成跳转
      if (r.kind === "page") {
        pushRecentPage(r.href);
        openApp(r.href);
      } else {
        void r.run();
      }
    },
    [close],
  );

  // ---------- 全局快捷键 ----------
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // ⌘K / Ctrl+K：任何时候都能唤起（含输入框内），这是编辑器类工具的通用约定
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((v) => {
          if (v) setQ("");
          return !v;
        });
        return;
      }
      if (e.key === "Escape" && open) {
        e.preventDefault();
        close();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, close]);

  useEffect(() => {
    if (open) {
      const t = setTimeout(() => inputRef.current?.focus(), 30);
      return () => clearTimeout(t);
    }
  }, [open]);

  // 选中项滚入可视区（键盘上下选择时）
  useEffect(() => {
    const el = listRef.current?.querySelector<HTMLElement>(`[data-idx="${cursor}"]`);
    el?.scrollIntoView({ block: "nearest" });
  }, [cursor, results.length]);

  // 分节展示。
  //
  // 空查询时按「最近访问 / 操作」分节，让老用户一按 ⌘K 就能回到刚才的地方；
  // 有查询时**故意不按分组拆散**——搜索场景下用户要的是"最匹配的排最前"，
  // 强行按分组归类会把高匹配项压到下面，反而更难找。
  //
  // 注意：这个 useMemo 必须放在 `if (!open) return null` 之前。
  // Hook 不能因为"面板关着"就被跳过，否则会破坏 Hook 调用顺序，
  // React 会直接抛错（这是 ESLint 唯一能提前发现的那类运行时崩溃）。
  const sections = useMemo<{ title: string; items: { r: Result; idx: number }[] }[]>(() => {
    if (q.trim()) {
      return [{ title: "搜索结果", items: results.map((r, idx) => ({ r, idx })) }];
    }
    const secs = [
      { title: "最近访问", items: [] as { r: Result; idx: number }[] },
      { title: "操作", items: [] as { r: Result; idx: number }[] },
    ];
    results.forEach((r, idx) => {
      const target = r.kind === "page" ? secs[0] : secs[1];
      target.items.push({ r, idx });
    });
    return secs.filter((s) => s.items.length);
  }, [q, results]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-[9998] flex items-start justify-center pt-[12vh] px-4"
      style={{ background: "rgba(4,4,8,0.6)", backdropFilter: "blur(3px)" }}
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) close();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="全局搜索与跳转"
        className="w-full max-w-xl rounded-2xl border shadow-2xl overflow-hidden animate-fade-up"
        style={{ background: "linear-gradient(180deg, #16161f, #101018)", borderColor: "var(--border)" }}
      >
        {/* 输入区 */}
        <div className="flex items-center gap-3 px-4 py-3 border-b" style={{ borderColor: "var(--border)" }}>
          <span aria-hidden className="text-sm" style={{ color: "var(--muted)" }}>🔍</span>
          <input
            ref={inputRef}
            className="flex-1 bg-transparent outline-none text-sm placeholder:text-[#55555f]"
            style={{ color: "var(--foreground)" }}
            placeholder="搜索页面或操作…  试试「模型」「token」「renwu」"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "ArrowDown") {
                e.preventDefault();
                setCursor((c) => (results.length ? (c + 1) % results.length : 0));
              } else if (e.key === "ArrowUp") {
                e.preventDefault();
                setCursor((c) => (results.length ? (c - 1 + results.length) % results.length : 0));
              } else if (e.key === "Enter") {
                e.preventDefault();
                go(results[cursor]);
              }
            }}
            aria-label="搜索页面或操作"
            aria-controls="cmdk-results"
            aria-activedescendant={results[cursor] ? `cmdk-${results[cursor].id}` : undefined}
          />
          <kbd
            className="shrink-0 text-[10px] px-1.5 py-0.5 rounded border"
            style={{ borderColor: "var(--border)", color: "var(--muted)" }}
          >
            Esc
          </kbd>
        </div>

        {/* 结果区 */}
        <div ref={listRef} id="cmdk-results" role="listbox" className="max-h-[52vh] overflow-y-auto py-1.5">
          {!results.length && (
            <div className="px-4 py-8 text-center text-sm" style={{ color: "var(--muted)" }}>
              没有找到「{q}」相关的页面
              <div className="text-xs mt-1.5 opacity-70">
                可以试试它的职责，比如「模型」「日志」「花了多少钱」
              </div>
            </div>
          )}

          {sections.map((g) => (
            <div key={g.title}>
              <div
                className="px-4 pt-2.5 pb-1 text-[10px] font-semibold tracking-widest uppercase"
                style={{ color: "#55555f" }}
              >
                {g.title}
              </div>
              {g.items.map(({ r, idx }) => {
                const on = idx === cursor;
                return (
                  <div
                    key={r.id}
                    id={`cmdk-${r.id}`}
                    data-idx={idx}
                    role="option"
                    aria-selected={on}
                    onMouseMove={() => setCursor(idx)}
                    onMouseDown={(e) => {
                      // 用 mousedown 触发：click 会先让输入框失焦，行为不稳定
                      e.preventDefault();
                      go(r);
                    }}
                    className="mx-1.5 px-2.5 py-2 rounded-lg flex items-center gap-3 cursor-pointer"
                    style={
                      on
                        ? { background: "linear-gradient(90deg, rgba(99,102,241,0.20), rgba(99,102,241,0.05))" }
                        : undefined
                    }
                  >
                    <span aria-hidden className="text-sm w-5 text-center shrink-0 opacity-80">{r.icon}</span>
                    <div className="min-w-0 flex-1">
                      <div className="text-sm truncate" style={{ color: on ? "#fff" : "var(--foreground)" }}>
                        {r.label}
                      </div>
                      <div className="text-[11px] truncate" style={{ color: "var(--muted)" }}>{r.desc}</div>
                    </div>
                    <span className="shrink-0 text-[10px]" style={{ color: "#55555f" }}>
                      {r.kind === "page" ? r.href : "操作"}
                    </span>
                  </div>
                );
              })}
            </div>
          ))}
        </div>

        {/* 键盘提示：让用户知道有更快的用法 */}
        <div
          className="flex items-center gap-4 px-4 py-2 border-t text-[11px]"
          style={{ borderColor: "var(--border)", color: "var(--muted)" }}
        >
          <span><kbd className="opacity-80">↑↓</kbd> 选择</span>
          <span><kbd className="opacity-80">↵</kbd> 打开</span>
          <span><kbd className="opacity-80">Esc</kbd> 关闭</span>
          <span className="ml-auto opacity-70">{ALL_PAGES.length - 1} 个页面可直达 · 开在右栏</span>
        </div>
      </div>
    </div>
  );
}
