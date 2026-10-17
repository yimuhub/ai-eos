#!/usr/bin/env python3
"""AI-EOS 专家人设批量原创重写生成器（商业化合规版）

合规核心原则（不可违背）：
  - 输入 LLM 的只有「职业名 + 部门 + emoji」这类【事实/概念】层信息；
  - 绝不把上游 agency-agents-zh 的任何正文喂进 prompt（避免构成演绎作品）；
  - LLM 用自己的知识独立创作 8 章节人设，产物 = AI-EOS 原创表达；
  - 生成后对上游原文做 8-gram 相似度门，若出现明显照搬（含 10-gram 重复）自动重生成；
  - 所有产物 source=ai-eos-original / license=AI-EOS 原创，上游 LICENSE 仅合规留档。

输出：content/experts/{slug}.md（frontmatter + 8 章节，可被既有 expert_library 解析器直接导入）
进度：tools/expert_rewrite_state.json（可断点续跑，按批次推进）
"""
import os, re, json, time, argparse, hashlib, random, sys
import httpx

BASE = os.path.expanduser("~/Documents/张阗/ai-eos")
VENDOR = os.path.join(BASE, "vendor", "agency-agents-zh")
OUTDIR = os.path.join(BASE, "content", "experts")
STATE = os.path.join(BASE, "tools", "expert_rewrite_state.json")
BRIEFS = os.path.join(BASE, "tools", "expert_briefs.json")

DEPT_CN = {
    "company": "公司高管", "engineering": "工程研发", "design": "设计",
    "marketing": "市场营销", "paid-media": "付费投放", "sales": "销售",
    "finance": "金融财务", "hr": "人力资源", "legal": "法务合规",
    "supply-chain": "供应链", "product": "产品", "project-management": "项目管理",
    "testing": "测试质量", "support": "客户支持", "specialized": "专项职能",
    "academic": "学术研究", "gis": "地理空间", "security": "安全",
    "strategy": "战略", "game-development": "游戏开发",
    "spatial-computing": "空间计算", "integrations": "系统集成",
}

# 我们的 8 章节模板（原创，供 LLM 遵守结构）
SECTION_TEMPLATE = """你是一位专业人设设定师。请为下列【单一职业】撰写一份完整的 AI Agent 人设档案。

## 要设定的职业
- 职业名称：{name}
- 所属部门：{dept_cn}
- 用这个 emoji 代表 TA：{emoji}

## 硬性要求
1. 这是【AI-EOS 团队原创】人设，必须用你自己的语言完整撰写，不得套用、改写或翻译任何已有文本。
2. 用简体中文，语气专业但克制，避免空话套话堆砌，写给"会实际雇佣这个 Agent 的老板"看。
3. 严格按下面的 Markdown 结构输出，章节标题一字不差（含 emoji 前缀）：

---
name: {name}
emoji: {emoji}
description: <用一句话（≤60字）概括这个职业最核心的价值主张>
color: <从 indigo / cyan / emerald / amber / rose / violet / blue / teal / orange / pink 里挑一个最贴合的>
---

# {emoji} {name}

> <一句点出该职业第一性原理的话，像座右铭，≤80字>

## 🧠 你的身份与记忆
<2-4 句介绍你是谁、服务谁、站在什么视角；再列 4-6 条"你记住什么"（你持续关注的上下文）>

## 🎯 你的核心使命
<1-2 句使命陈述 + 用列表列出你覆盖的 5-8 个核心工作领域（加粗小标题 + 冒号 + 一句说明）>

## 🚨 你遵守的关键规则
<列出 5-8 条硬规则，用无序列表，每条"加粗规则名：一句怎么做"。要具体、可执行，不要原则性空话>

## 📦 你的交付物
<列出 5-8 项你产出的具体交付物，用无序列表，每项"加粗名称：一句它长什么样/给谁用"。这是验收要依据的东西，务必具体>

## 🔄 你的工作流程
<用 1. 2. 3. … 列出 5-8 步标准作业流程，每步一行>

## 💬 你的沟通风格
<3-4 句描述你怎么表达：对老板怎么汇报、遇到不确定怎么办、讨厌什么表达>

## 📈 你的成功指标
<列出 4-6 条可量化的"做得好不好"指标，用无序列表>

## ⚡ 你的进阶能力
<列出 3-5 项进阶能力/边界（你会主动做什么延伸、什么会明确交给别人）>

## 重要
- 只输出上面这份 Markdown，不要加任何解释、前言、总结。
- 内容要【专属于这个职业】，不要写成放之四海皆准的通用模板。"""


def llm_key():
    return os.environ.get("HERMES_CUSTOM_CUSTOM_API_KEY", "").strip()


# ---- 全局 429 冷却：任何线程撞 429 都让所有线程睡一会儿 ----
# Event 初始必须 set()（代表"未冷却"）；_cool() 会 clear() 让所有 _wait_cool() 停住
import threading
_COOL = threading.Event()
_COOL.set()
_coil_lock = threading.Lock()

def _cool(seconds=5):
    with _coil_lock:
        _COOL.clear()
    time.sleep(seconds)
    _COOL.set()

def _wait_cool():
    while not _COOL.is_set():
        time.sleep(0.3)


def llm_complete(client, prompt, model, max_tokens=2800, temperature=0.9):
    _wait_cool()
    last = None
    for try_i in range(5):  # 429/5xx 指数退避
        r = client.post(
            "/chat/completions",
            headers={"Authorization": f"Bearer {llm_key()}"},
            json={"model": model,
                  "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": max_tokens, "temperature": temperature},
            timeout=150,
        )
        if r.status_code == 429 or r.status_code >= 500:
            last = f"{r.status_code}"
            backoff = [3, 8, 15, 30, 45][try_i]
            if r.status_code == 429:
                _cool(backoff)  # 全员冷却，避免继续撞墙
            time.sleep(backoff)
            continue
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    raise RuntimeError(f"LLM 多次重试仍失败（最后状态 {last}）")


def ngrams(text, n):
    text = re.sub(r"\s+", "", text)
    return {text[i:i + n] for i in range(max(0, len(text) - n + 1)) if len(text[i:i + n]) == n}


def similarity_gate(generated, upstream_text, gram=10, threshold=0.02):
    """上游 10-gram 有多少出现在生成文本里。我们没喂正文，正常应≈0。"""
    if not upstream_text:
        return 0.0, 0
    up = ngrams(upstream_text, gram)
    if not up:
        return 0.0, 0
    gen = ngrams(generated, gram)
    hit = up & gen
    ratio = len(hit) / len(up)
    return ratio, len(hit)


def _clean_llm_md(raw, name, emoji):
    """把 LLM 输出规整成合法 frontmatter + body。"""
    raw = raw.strip()
    # 去掉可能的 ```markdown 围栏
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
    # 确保以 frontmatter 开头
    if not raw.startswith("---"):
        raw = f"---\nname: {name}\nemoji: {emoji}\ndescription: {name}\ncolor: indigo\n---\n\n{raw}"
    return raw


def make_prompt(brief):
    return SECTION_TEMPLATE.format(
        name=brief["name"],
        dept_cn=DEPT_CN.get(brief["dept"], brief["dept"]),
        emoji=brief.get("emoji") or "🧑‍💼",
    )


def upstream_text(slug):
    """相似度门基线：主目录（vendor/agency-agents-zh）或瘦身后的存档目录。"""
    for base in (VENDOR, VENDOR + "-archive"):
        p = os.path.join(base, slug + ".md")
        if os.path.isfile(p):
            return open(p, encoding="utf-8").read()
    return ""


def load_state():
    if os.path.exists(STATE):
        return json.load(open(STATE))
    return {"done": {}, "failed": {}, "model": "agnes-2.5-flash"}


def save_state(st):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(st, open(STATE, "w"), ensure_ascii=False, indent=1)


def process_one(brief, client, model, gram=10, threshold=0.02, max_retry=2):
    """纯函数：生成一个专家，返回 (ok, attempt, ratio, hit, record)。
    不碰共享 state —— 由主线程拿到 record 后统一落盘，避免竞态。"""
    slug = brief["slug"]
    up = upstream_text(slug)
    prompt = make_prompt(brief)
    raw, ratio, hit = "", 0.0, 0
    for attempt in range(max_retry + 1):
        try:
            raw = llm_complete(client, prompt, model, temperature=0.9 + 0.1 * attempt)
        except Exception as e:
            return False, attempt + 1, 0.0, 0, \
                {"reason": f"llm_error {type(e).__name__}: {str(e)[:120]}", "attempts": attempt + 1}
        raw = _clean_llm_md(raw, brief["name"], brief.get("emoji") or "🧑‍💼")
        ratio, hit = similarity_gate(raw, up, gram=gram, threshold=threshold)
        if ratio <= threshold:
            path = os.path.join(OUTDIR, slug + ".md")
            os.makedirs(OUTDIR, exist_ok=True)
            open(path, "w", encoding="utf-8").write(raw + "\n")
            return True, attempt + 1, ratio, hit, {
                "attempt": attempt + 1, "chars": len(raw),
                "gate10gram_hit": hit, "gate_ratio": round(ratio, 4),
                "sha8": hashlib.sha256(raw.encode()).hexdigest()[:8],
                "ts": int(time.time()),
            }
        # 超阈值 → 重生成（更高温度打散）
    path = os.path.join(OUTDIR, slug + ".md")
    os.makedirs(OUTDIR, exist_ok=True)
    open(path, "w", encoding="utf-8").write(raw + "\n")
    return False, max_retry + 1, ratio, hit, \
        {"reason": f"similarity {ratio:.3f} > {threshold}", "attempts": max_retry + 1}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=0, help="本批处理多少个；0=全部待办")
    ap.add_argument("--workers", type=int, default=6, help="并发数（LLM API 并发调用）")
    ap.add_argument("--slugs", help="逗号分隔的指定 slug；缺省按 briefs 顺序取未完成的")
    ap.add_argument("--model", default="agnes-2.5-flash")
    ap.add_argument("--list", action="store_true", help="只打印待办/已完成统计，不生成")
    args = ap.parse_args()

    briefs = json.load(open(BRIEFS))
    by_slug = {b["slug"]: b for b in briefs}
    st = load_state()
    st["model"] = args.model
    pending = [s for s in by_slug if s not in st["done"]]
    print(f"总专家 {len(by_slug)} | 已完成 {len(st['done'])} | 失败 {len(st['failed'])} | 待办 {len(pending)}")

    if args.list:
        print(f"待办 {len(pending)} 个，前 20: {pending[:20]}")
        return

    if not llm_key():
        sys.exit("❌ 找不到 LLM key（HERMES_CUSTOM_CUSTOM_API_KEY）")

    if args.slugs:
        slugs = [s.strip() for s in args.slugs.split(",") if s.strip() in by_slug]
    else:
        slugs = pending[:args.batch] if args.batch else pending
    print(f"本批处理 {len(slugs)} 个，并发 {args.workers}，前 6: {[s for s in slugs][:6]}…")

    os.makedirs(OUTDIR, exist_ok=True)
    base = "https://api.agnes-ai.cn/v1"
    from concurrent.futures import ThreadPoolExecutor, as_completed
    lock = threading.Lock()

    def run_one(slug):
        with httpx.Client(base_url=base) as client:
            ok, att, ratio, hit, record = process_one(by_slug[slug], client, args.model,
                                                       gram=10, threshold=0.02, max_retry=2)
        mark = "✅" if ok else "⚠️ 待人工"
        print(f"{mark} {slug}  (试{att}次 相似度{ratio:.3f} 命中{hit} 10gram)")
        return slug, ok, record

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futs = {ex.submit(run_one, s): s for s in slugs}
        done_cnt = 0
        for fut in as_completed(futs):
            try:
                slug, ok, record = fut.result()
            except Exception as e:
                slug, ok, record = futs[fut], False, {"reason": f"exception {type(e).__name__}: {str(e)[:120]}"}
                print(f"❌ {slug} 异常: {type(e).__name__} {str(e)[:100]}")
            # 主线程持锁统一写 state（线程只返回结果，绝不碰共享 dict）
            with lock:
                if ok:
                    st["done"][slug] = record
                    st["failed"].pop(slug, None)
                else:
                    st["failed"][slug] = record
                save_state(st)
            done_cnt += 1
            time.sleep(0.15)

    save_state(st)
    print(f"\n本批结束。累计完成 {len(st['done'])}/{len(by_slug)}，失败 {len(st['failed'])}")
    if st["failed"]:
        print("失败清单:", list(st["failed"].keys()))


if __name__ == "__main__":
    main()
