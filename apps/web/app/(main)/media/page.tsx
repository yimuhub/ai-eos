"use client";

// 创作中心：图片 / 视频生成
//
// 模型目录里接入的图片/视频模型在这里真正用起来：
//   · 图片：选模型 → 写提示词 → 同步等上游出图（约 10–30 秒）；
//   · 视频：提交后上游异步渲染，页面每 3 秒轮询一次直到完成；
//   · 结果自动下载到公司存储，历史可回看、可下载、可删除。
//
// 接口：
//   GET  /models?company_id=            （过滤 image/video 类型）
//   POST /media/images                  { company_id, model_id, prompt, size }
//   POST /media/videos                  { company_id, model_id, prompt, seconds, aspect_ratio }
//   GET  /media/videos/{id}             （轮询进度）
//   GET  /media/generations?company_id= （历史）
//   DELETE /media/generations/{id}

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { api, describeError, downloadURL, fetchBlobURL } from "@/lib/api";
import { useActiveCompanyId, useResource, useVisibilityInterval } from "@/lib/useResource";
import { notify } from "@/components/Toast";
import { confirmAction } from "@/components/ConfirmDialog";

type ModelItem = {
  id: string;
  name: string;
  model_id: string;
  model_type?: string | null;
  enabled?: boolean | null;
  company_id?: string | null;
};

type Generation = {
  id: string;
  kind: "image" | "video";
  model_name: string;
  prompt: string;
  status: "processing" | "completed" | "failed";
  params: Record<string, unknown>;
  source_url?: string | null;
  file_url?: string | null;
  progress?: number | null;
  error?: string | null;
  created_at?: string | null;
};

const IMAGE_SIZES = ["1024x1024", "1536x1024", "1024x1536"];

// 结果文件接口需要 Bearer 鉴权，<img src>/<a href> 直连带不上 token（会 401）。
// 与素材资产页一致：先带鉴权取回 blob，再用本地 objectURL 渲染/下载。
function MediaPreview({ g, className }: { g: Generation; className: string }) {
  const [url, setUrl] = useState<string | null>(null);
  const path = g.file_url;
  useEffect(() => {
    if (!path) return;
    let revoked: string | null = null;
    let alive = true;
    fetchBlobURL(path, { silent: true })
      .then((u) => { if (alive) { revoked = u; setUrl(u); } else URL.revokeObjectURL(u); })
      .catch(() => { if (alive) setUrl(null); });
    return () => { alive = false; if (revoked) URL.revokeObjectURL(revoked); };
  }, [path]);
  if (!url) {
    return (
      <div className={`${className} flex items-center justify-center text-xs`}
        style={{ background: "var(--surface-2)", color: "var(--muted)" }}>
        加载预览…
      </div>
    );
  }
  return g.kind === "image" ? (
    // eslint-disable-next-line @next/next/no-img-element
    <img src={url} alt={g.prompt} className={className} />
  ) : (
    <video src={url} controls preload="metadata" className={className} />
  );
}

async function downloadGeneration(g: Generation) {
  if (!g.file_url) return;
  try {
    const url = await fetchBlobURL(g.file_url);
    downloadURL(url, `${g.kind}-${g.id.slice(0, 8)}.${g.kind === "image" ? "png" : "mp4"}`);
  } catch (e) {
    notify("error", describeError(e, "下载失败"));
  }
}

function StatusBadge({ g }: { g: Generation }) {
  if (g.status === "completed")
    return <span className="badge" style={{ color: "#4ade80", borderColor: "#4ade8055" }}>已完成</span>;
  if (g.status === "failed")
    return <span className="badge" style={{ color: "#f87171", borderColor: "#f8717155" }}>失败</span>;
  return (
    <span className="badge" style={{ color: "var(--accent-2)", borderColor: "#22d3ee55" }}>
      生成中{typeof g.progress === "number" ? ` ${g.progress}%` : "…"}
    </span>
  );
}

export default function MediaPage() {
  const companyId = useActiveCompanyId();
  const [tab, setTab] = useState<"image" | "video">("image");
  const [modelId, setModelId] = useState("");
  const [prompt, setPrompt] = useState("");
  const [size, setSize] = useState("1024x1024");
  const [seconds, setSeconds] = useState("5");
  const [ratio, setRatio] = useState("16:9");
  const [busy, setBusy] = useState(false);
  const [latest, setLatest] = useState<Generation | null>(null);
  const [formError, setFormError] = useState<string | null>(null);

  const models = useResource<{ items?: ModelItem[] } | ModelItem[]>(
    companyId ? `models:${companyId}` : null,
    () => api.get(`/models?company_id=${companyId}`),
  );
  const history = useResource<{ items: Generation[] }>(
    companyId ? `media-history:${companyId}` : null,
    () => api.get(`/media/generations?company_id=${companyId}`),
  );

  const modelList = useMemo(() => {
    const raw = models.data;
    const items = Array.isArray(raw) ? raw : (raw?.items ?? []);
    return items.filter(
      (m) => (m.model_type ?? "text") === tab && m.enabled !== false,
    );
  }, [models.data, tab]);

  // 切换标签/公司后，若当前选中模型不在候选里，自动选第一个
  useEffect(() => {
    if (modelList.length === 0) { setModelId(""); return; }
    if (!modelList.some((m) => m.id === modelId)) setModelId(modelList[0].id);
  }, [modelList, modelId]);

  // 最新的进行中视频：每 3 秒轮询（页面隐藏时自动暂停）
  const pendingVideo = useMemo(() => {
    const items = history.data?.items ?? [];
    const fromLatest = latest && latest.kind === "video" && latest.status === "processing" ? latest : null;
    return fromLatest ?? items.find((g) => g.kind === "video" && g.status === "processing") ?? null;
  }, [history.data, latest]);

  const pollPending = useCallback(async () => {
    if (!pendingVideo) return;
    try {
      const d = await api.get<Generation>(`/media/videos/${pendingVideo.id}`, { silent: true });
      setLatest((cur) => (cur && cur.id === d.id ? d : cur));
      if (d.status !== "processing") {
        history.refresh();
        if (d.status === "completed") notify("success", "视频生成完成");
        else notify("error", `视频生成失败：${d.error ?? "未知原因"}`);
      } else {
        history.mutate({
          items: (history.data?.items ?? []).map((g) => (g.id === d.id ? d : g)),
        });
      }
    } catch {
      // 轮询失败静默：下一轮再试，避免后台标签页刷错误提示
    }
  }, [pendingVideo, history]);

  useVisibilityInterval(() => { void pollPending(); }, 3000);

  async function generate() {
    if (!companyId) return;
    if (!modelId) { setFormError("请先选择模型；没有可用模型时到「模型配置」接入图片/视频模型。"); return; }
    if (!prompt.trim()) { setFormError("请填写提示词，描述你想生成的画面。"); return; }
    setBusy(true);
    setFormError(null);
    try {
      if (tab === "image") {
        const d = await api.post<Generation>("/media/images", {
          company_id: companyId, model_id: modelId, prompt: prompt.trim(), size,
        });
        setLatest(d);
        notify("success", "图片生成完成");
      } else {
        const d = await api.post<Generation>("/media/videos", {
          company_id: companyId, model_id: modelId, prompt: prompt.trim(),
          seconds, aspect_ratio: ratio,
        });
        setLatest(d);
        notify("info", "视频任务已提交，渲染中（通常 1–3 分钟）");
      }
      history.refresh();
    } catch (e) {
      setFormError(describeError(e, "生成失败"));
    } finally {
      setBusy(false);
    }
  }

  async function remove(g: Generation) {
    const ok = await confirmAction({
      title: "删除这条生成记录？",
      description: `将删除这条${g.kind === "image" ? "图片" : "视频"}记录及本地文件，不可恢复。`,
      confirmText: "删除",
      danger: true,
    });
    if (!ok) return;
    try {
      await api.del(`/media/generations/${g.id}`);
      if (latest?.id === g.id) setLatest(null);
      history.refresh();
      notify("success", "已删除");
    } catch (e) {
      notify("error", describeError(e, "删除失败"));
    }
  }

  const items = history.data?.items ?? [];

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up space-y-5">
      <div className="flex items-end justify-between gap-3 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">🎨 创作中心</h1>
          <p className="text-sm mt-1" style={{ color: "var(--muted)" }}>
            用接入的图片/视频模型生成素材，结果自动存进公司资产，可回看、可下载。
          </p>
        </div>
        <button onClick={() => history.refresh()} className="btn-ghost text-xs">刷新</button>
      </div>

      <div className="card card-static p-5 space-y-4">
        <div className="flex gap-2">
          {(["image", "video"] as const).map((k) => (
            <button
              key={k}
              onClick={() => setTab(k)}
              className={tab === k ? "btn-primary text-xs" : "btn-ghost text-xs"}
            >
              {k === "image" ? "🖼 图片生成" : "🎬 视频生成"}
            </button>
          ))}
        </div>

        <div className="grid gap-4 md:grid-cols-[280px_1fr]">
          <div className="space-y-3">
            <label className="block">
              <span className="text-xs" style={{ color: "var(--muted)" }}>模型</span>
              <select className="input mt-1" value={modelId} onChange={(e) => setModelId(e.target.value)}>
                {modelList.length === 0 && <option value="">暂无可用{tab === "image" ? "图片" : "视频"}模型</option>}
                {modelList.map((m) => (
                  <option key={m.id} value={m.id}>{m.name}</option>
                ))}
              </select>
            </label>
            {tab === "image" ? (
              <label className="block">
                <span className="text-xs" style={{ color: "var(--muted)" }}>尺寸</span>
                <select className="input mt-1" value={size} onChange={(e) => setSize(e.target.value)}>
                  {IMAGE_SIZES.map((s) => <option key={s} value={s}>{s}</option>)}
                </select>
              </label>
            ) : (
              <>
                <label className="block">
                  <span className="text-xs" style={{ color: "var(--muted)" }}>时长（秒）</span>
                  <select className="input mt-1" value={seconds} onChange={(e) => setSeconds(e.target.value)}>
                    {["4", "5", "6", "8", "10", "12"].map((s) => <option key={s} value={s}>{s} 秒</option>)}
                  </select>
                </label>
                <label className="block">
                  <span className="text-xs" style={{ color: "var(--muted)" }}>画幅</span>
                  <select className="input mt-1" value={ratio} onChange={(e) => setRatio(e.target.value)}>
                    <option value="16:9">16:9 横屏</option>
                    <option value="9:16">9:16 竖屏</option>
                    <option value="1:1">1:1 方形</option>
                  </select>
                </label>
              </>
            )}
            {modelList.length === 0 && !models.loading && (
              <p className="text-xs leading-5" style={{ color: "var(--muted)" }}>
                还没有{tab === "image" ? "图片" : "视频"}模型。去{" "}
                <Link href="/models" className="underline" style={{ color: "var(--accent)" }}>模型配置</Link>{" "}
                接入供应商并导入对应类型的模型。
              </p>
            )}
          </div>
          <div className="space-y-3">
            <textarea
              className="input min-h-[120px] resize-y"
              placeholder={tab === "image"
                ? "描述画面，例如：一只橘猫坐在办公桌前用笔记本电脑，扁平插画风格，暖色调"
                : "描述视频画面与镜头，例如：无人机视角掠过清晨的城市天际线，镜头缓慢推进"}
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              maxLength={4000}
            />
            <div className="flex items-center gap-3">
              <button className="btn-primary text-sm" disabled={busy || !companyId} onClick={() => void generate()}>
                {busy ? "生成中…" : tab === "image" ? "生成图片" : "提交视频任务"}
              </button>
              <span className="text-xs" style={{ color: "var(--muted)" }}>
                {tab === "image" ? "通常 10–30 秒出图" : "上游异步渲染，通常 1–3 分钟"}
              </span>
            </div>
            {formError && (
              <p className="text-xs leading-5" style={{ color: "#f87171" }}>{formError}</p>
            )}
          </div>
        </div>

        {latest && (
          <div className="border-t pt-4 space-y-2" style={{ borderColor: "var(--border)" }}>
            <div className="flex items-center gap-2 text-xs" style={{ color: "var(--muted)" }}>
              <span>本次结果</span><StatusBadge g={latest} />
              <span>{latest.model_name}</span>
            </div>
            {latest.status === "completed" && latest.file_url && (
              <MediaPreview g={latest} className="max-h-[420px] rounded-lg border" />
            )}
            {latest.status === "failed" && (
              <p className="text-xs" style={{ color: "#f87171" }}>{latest.error}</p>
            )}
          </div>
        )}
      </div>

      <div className="space-y-3">
        <h2 className="text-sm font-semibold" style={{ color: "var(--muted)" }}>
          生成历史{items.length > 0 ? `（${items.length}）` : ""}
        </h2>
        {history.loading && !history.data && (
          <p className="text-sm" style={{ color: "var(--muted)" }}>加载中…</p>
        )}
        {history.error && (
          <p className="text-sm" style={{ color: "#f87171" }}>{history.error}</p>
        )}
        {!history.loading && items.length === 0 && (
          <div className="card card-static p-8 text-center text-sm" style={{ color: "var(--muted)" }}>
            还没有生成记录。选一个模型、写一句提示词，开始第一次创作。
          </div>
        )}
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {items.map((g) => (
            <div key={g.id} className="card card-static p-3 space-y-2">
              <div className="flex items-center justify-between gap-2">
                <StatusBadge g={g} />
                <span className="text-[11px] truncate" style={{ color: "var(--muted)" }}>
                  {g.kind === "image" ? "🖼" : "🎬"} {g.model_name}
                </span>
              </div>
              {g.status === "completed" && g.file_url ? (
                <MediaPreview g={g} className={g.kind === "image" ? "w-full h-40 object-cover rounded-md" : "w-full h-40 rounded-md bg-black"} />
              ) : (
                <div className="w-full h-40 rounded-md flex items-center justify-center text-xs"
                  style={{ background: "var(--surface-2)", color: "var(--muted)" }}>
                  {g.status === "processing" ? "渲染中…" : "无预览"}
                </div>
              )}
              <p className="text-xs leading-5 line-clamp-2" title={g.prompt}>{g.prompt}</p>
              {g.error && <p className="text-[11px] line-clamp-2" style={{ color: "#f87171" }}>{g.error}</p>}
              <div className="flex items-center justify-between">
                <span className="text-[10px]" style={{ color: "var(--muted)" }}>
                  {g.created_at ? new Date(g.created_at).toLocaleString() : ""}
                </span>
                <div className="flex gap-2">
                  {g.status === "completed" && g.file_url && (
                    <button className="btn-ghost text-[11px] !px-2 !py-1" onClick={() => void downloadGeneration(g)}>下载</button>
                  )}
                  <button className="btn-ghost text-[11px] !px-2 !py-1" onClick={() => void remove(g)}>删除</button>
                </div>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}