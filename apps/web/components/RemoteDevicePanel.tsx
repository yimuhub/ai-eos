"use client";
import { useState } from "react";
import { API_BASE, api, describeError } from "@/lib/api";
import { confirmAction } from "@/components/ConfirmDialog";
import {
  buildSidecarCommand,
  deviceTone,
  dispatchStatusLabel,
  formatHeartbeat,
  type RemoteDevice,
} from "@/lib/remoteDevices";

/* ============================================================
   远程执行设备：把「动手」搬到目标机器上。

   后端只判该不该做，sidecar 在那台机器上真做。这一面板负责的是老板侧的
   四件事：登记设备（拿到一次性接入码 + 可直接复制的启动命令）、看谁在线、
   设为默认执行机、吊销。

   接入码只显示一次（库里存哈希），所以创建成功后必须把命令摊在页面上，
   而不是"复制一下就再也找不回来"。
   ============================================================ */

export type DeviceList = {
  devices: RemoteDevice[];
  default_device_id?: string | null;
  online_window_seconds?: number;
};

export type DispatchRow = {
  id: string;
  device_id: string;
  capability: string;
  status: string;
  error?: string | null;
  created_at?: string | null;
  finished_at?: string | null;
};

type Props = {
  cid: string;
  devices: RemoteDevice[];
  defaultDeviceId?: string | null;
  onlineWindowSeconds?: number;
  refresh: () => void;
};

const CAP_LABELS: Record<string, string> = {
  run_shell: "Shell 命令",
  read_file: "读文件",
  write_file: "写文件",
  apply_patch: "打补丁",
};

export default function RemoteDevicePanel({
  cid, devices, defaultDeviceId, onlineWindowSeconds, refresh,
}: Props) {
  const [name, setName] = useState("");
  const [allowRoot, setAllowRoot] = useState("");
  // 新建/重发接入码后停在这里，等老板复制完
  const [issued, setIssued] = useState<{ deviceId: string; code: string; name: string } | null>(null);
  const [dispatches, setDispatches] = useState<DispatchRow[]>([]);
  const [showQueue, setShowQueue] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [copied, setCopied] = useState(false);

  const command = issued
    ? buildSidecarCommand({ server: API_BASE, code: issued.code, allowRoot })
    : "";

  async function create() {
    if (busy || !name.trim()) return;
    setBusy(true); setMsg("");
    try {
      const out = await api.post<{ device: RemoteDevice; enroll_code: string; hint?: string }>(
        `/computer-devices?company_id=${cid}`, { name: name.trim() });
      setIssued({ deviceId: out.device.id, code: out.enroll_code, name: out.device.name });
      setName("");
      refresh();
    } catch (e) { setMsg(`❌ ${describeError(e, "登记失败")}`); }
    setBusy(false);
  }

  async function renew(device: RemoteDevice) {
    if (busy) return;
    setBusy(true); setMsg("");
    try {
      const out = await api.post<{ enroll_code: string }>(
        `/computer-devices/${device.id}/renew?company_id=${cid}`);
      setIssued({ deviceId: device.id, code: out.enroll_code, name: device.name });
      refresh();
    } catch (e) { setMsg(`❌ ${describeError(e, "重新生成失败")}`); }
    setBusy(false);
  }

  async function revoke(device: RemoteDevice) {
    if (busy) return;
    const ok = await confirmAction({
      title: `吊销「${device.name}」？`,
      description: "那台机器上的 sidecar 会立刻领不到命令，之后要用得重新生成接入码接入。",
      bullets: [
        "排队中的命令会被作废",
        "如果它是默认执行机，公司会自动取消默认（回到本机直连）",
      ],
      confirmText: "吊销",
      danger: true,
    });
    if (!ok) return;
    setBusy(true); setMsg("");
    try {
      const out = await api.post<{ cancelled_dispatches: number }>(
        `/computer-devices/${device.id}/revoke?company_id=${cid}`);
      setMsg(`已吊销，同时作废排队中的命令 ${out.cancelled_dispatches} 条`);
      if (issued?.deviceId === device.id) setIssued(null);
      refresh();
    } catch (e) { setMsg(`❌ ${describeError(e, "吊销失败")}`); }
    setBusy(false);
  }

  async function setDefault(device: RemoteDevice) {
    setBusy(true); setMsg("");
    try {
      await api.post(`/computer-devices/${device.id}/set-default?company_id=${cid}`);
      refresh();
    } catch (e) { setMsg(`❌ ${describeError(e, "设为默认失败")}`); }
    setBusy(false);
  }

  async function clearDefault(device: RemoteDevice) {
    setBusy(true); setMsg("");
    try {
      await api.post(`/computer-devices/${device.id}/clear-default?company_id=${cid}`);
      refresh();
    } catch (e) { setMsg(`❌ ${describeError(e, "取消默认失败")}`); }
    setBusy(false);
  }

  async function toggleQueue() {
    if (showQueue) { setShowQueue(false); return; }
    setShowQueue(true);
    try {
      const out = await api.get<{ dispatches: DispatchRow[] }>(
        `/computer-devices/dispatches?company_id=${cid}&limit=20`);
      setDispatches(out.dispatches || []);
    } catch (e) { setMsg(`❌ ${describeError(e, "队列读取失败")}`); }
  }

  async function copyCommand() {
    try {
      await navigator.clipboard.writeText(command);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      setMsg("❌ 浏览器不允许自动复制，请手动选中上面两行复制");
    }
  }

  const window_ = onlineWindowSeconds ?? 45;

  return (
    <div className="card card-static">
      <h2 className="font-semibold text-sm mb-1">🛰 远程执行设备</h2>
      <p className="text-[11px] mb-3 leading-relaxed" style={{ color: "var(--muted)" }}>
        让 AI 员工操作另一台电脑。命令在这里登记，真正的执行发生在那台机器上的
        sidecar 进程里——它还会用自己机器上的护栏再判一次（可写目录、只读命令白名单）。
      </p>

      {/* 新建设备 */}
      <div className="flex flex-wrap gap-2 mb-3">
        <input value={name} onChange={(e) => setName(e.target.value)}
          placeholder="设备名，如「老张的 MacBook」"
          className="input flex-1 min-w-[160px] !py-2 text-xs" />
        <input value={allowRoot} onChange={(e) => setAllowRoot(e.target.value)}
          placeholder="那台机器上的可写目录（可留空 = 只读）"
          className="input flex-1 min-w-[180px] !py-2 text-xs font-mono" />
        <button onClick={create} disabled={busy || !name.trim() || !cid}
          className="btn-primary !py-2 !px-4 text-xs shrink-0 disabled:opacity-40">
          登记设备
        </button>
      </div>

      {/* 一次性接入码 + 启动命令 */}
      {issued && (
        <div className="rounded-xl p-3 mb-3" style={{ background: "var(--surface-2)", border: "1px solid rgba(99,102,241,0.3)" }}>
          <p className="text-[11px] mb-2 font-medium">
            「{issued.name}」的接入码（只显示这一次，30 分钟内有效）
          </p>
          <pre className="text-[10px] whitespace-pre-wrap break-all font-mono mb-2"
            style={{ color: "var(--foreground)" }}>{command}</pre>
          <div className="flex gap-2">
            <button onClick={copyCommand}
              className="btn-ghost !py-1 !px-2.5 text-[11px]">
              {copied ? "✓ 已复制" : "复制命令"}
            </button>
            <button onClick={() => setIssued(null)}
              className="btn-ghost !py-1 !px-2.5 text-[11px]">
              收好了，关掉
            </button>
          </div>
          <p className="text-[10px] mt-2 leading-relaxed" style={{ color: "var(--muted)" }}>
            在那台机器上（装了 Python 3 就行，不用装依赖、不用拉仓库）依次跑这两行。
            没填可写目录的话那台设备是只读的：能读文件、能跑只读命令，
            写文件会被那台机器自己拒绝。
          </p>
        </div>
      )}

      {/* 设备列表 */}
      <div className="space-y-2">
        {devices.map((d) => {
          const tone = deviceTone(d);
          const isDefault = defaultDeviceId === d.id;
          return (
            <div key={d.id} className="rounded-xl px-3 py-2"
              style={{ background: "var(--surface-2)",
                       border: isDefault ? "1px solid rgba(99,102,241,0.45)" : "1px solid var(--border)" }}>
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-xs font-medium">{d.name}</span>
                <span className={`text-[10px] ${tone.className}`}>● {tone.label}</span>
                {isDefault && (
                  <span className="text-[10px] px-1.5 py-0.5 rounded bg-indigo-500/15 text-indigo-300">
                    默认执行机
                  </span>
                )}
                <span className="ml-auto text-[10px]" style={{ color: "var(--muted)" }}>
                  {formatHeartbeat(d.last_seen_at, Date.now())}
                </span>
              </div>
              <p className="text-[10px] mt-1 leading-relaxed" style={{ color: "var(--muted)" }}>
                {d.hostname || "未上报主机名"} · {d.platform || "未知平台"}
                {d.agent_version ? ` · sidecar ${d.agent_version}` : ""}
                {(d.capabilities || []).length > 0 && (
                  <> · 能力：{(d.capabilities || []).map((c) => CAP_LABELS[c] ?? c).join("、")}</>
                )}
              </p>
              <div className="flex flex-wrap gap-2 mt-2">
                {!d.revoked && d.enrolled && !isDefault && (
                  <button onClick={() => setDefault(d)} disabled={busy}
                    className="text-[11px] px-2 py-1 rounded-lg transition-colors"
                    style={{ border: "1px solid var(--border)", color: "var(--muted)" }}>
                    设为默认执行机
                  </button>
                )}
                {isDefault && (
                  <button onClick={() => clearDefault(d)} disabled={busy}
                    className="text-[11px] px-2 py-1 rounded-lg transition-colors"
                    style={{ border: "1px solid var(--border)", color: "var(--muted)" }}>
                    取消默认（回到本机直连）
                  </button>
                )}
                {!d.enrolled && (
                  <button onClick={() => renew(d)} disabled={busy}
                    className="text-[11px] px-2 py-1 rounded-lg transition-colors"
                    style={{ border: "1px solid var(--border)", color: "var(--muted)" }}>
                    重新生成接入码
                  </button>
                )}
                {!d.revoked && (
                  <button onClick={() => revoke(d)} disabled={busy}
                    className="text-[11px] px-2 py-1 rounded-lg text-red-300 transition-colors"
                    style={{ border: "1px solid rgba(239,68,68,0.3)" }}>
                    吊销
                  </button>
                )}
              </div>
            </div>
          );
        })}
        {!devices.length && (
          <p className="text-xs text-center py-3" style={{ color: "var(--muted)" }}>
            还没有设备。不登记也照样能用——所有操作默认在这台后端机器上执行。
          </p>
        )}
      </div>

      <div className="flex items-center gap-3 mt-3">
        <button onClick={toggleQueue} className="btn-ghost !py-1 !px-2.5 text-[11px]">
          {showQueue ? "收起下发队列" : "看下发队列"}
        </button>
        <p className="text-[10px]" style={{ color: "var(--muted)" }}>
          超过 {window_} 秒没有心跳就按离线处理；离线设备上的命令不会被回放到本机执行。
        </p>
      </div>

      {showQueue && (
        <div className="mt-2 space-y-1 max-h-48 overflow-y-auto">
          {dispatches.map((row) => (
            <div key={row.id} className="flex items-center gap-2 text-[10px] px-2 py-1.5 rounded-lg"
              style={{ background: "var(--surface-2)" }}>
              <span className="font-mono">{CAP_LABELS[row.capability] ?? row.capability}</span>
              <span style={{ color: row.status === "succeeded" ? "var(--muted)"
                : row.status === "expired" ? "#f87171" : "var(--muted)" }}>
                {dispatchStatusLabel(row.status)}
              </span>
              <span className="ml-auto shrink-0" style={{ color: "var(--muted)" }}>
                {devices.find((d) => d.id === row.device_id)?.name ?? row.device_id.slice(0, 8)}
              </span>
            </div>
          ))}
          {!dispatches.length && (
            <p className="text-[10px] text-center py-2" style={{ color: "var(--muted)" }}>
              队列里还没有命令
            </p>
          )}
        </div>
      )}

      {msg && <p className="mt-2 text-[11px] text-indigo-300">{msg}</p>}
    </div>
  );
}
