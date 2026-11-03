// 远程执行设备的展示与启动命令拼装（纯函数，可单测）。
//
// 单独成文件的原因有两个：
// 1. sidecar 的启动命令是**照了就能用**的东西——填错一个参数（比如把可写目录
//    漏掉），老板在设备上跑完才发现「写文件全被本地拒绝」，排查成本很高；
// 2. 在线/待接入/离线/已吊销四种状态的文案在设备列表、执行下拉、审计日志里
//    都要出现，散在三处必然写法不一致。

/** 设备一行（与 /computer-devices 的 DeviceListOut.devices 同构）。 */
export type RemoteDevice = {
  id: string;
  name: string;
  platform?: string | null;
  hostname?: string | null;
  agent_version?: string | null;
  capabilities?: string[] | null;
  enrolled?: boolean;
  online?: boolean;
  revoked?: boolean;
  last_seen_at?: string | null;
  enrollment_ready?: boolean;
};

export type DeviceTone = {
  label: string;
  /** tailwind 色调：只在这里出现一次，列表/下拉/日志共用 */
  className: string;
  dot: "online" | "offline" | "pending" | "revoked";
};

/** 状态判定顺序不能换：已吊销 > 没接入 > 在线 > 离线。
    「已吊销」的设备服务端仍会返回心跳时间，先判在线就会把黑名单画成绿点。 */
export function deviceTone(device: RemoteDevice): DeviceTone {
  if (device.revoked) {
    return { label: "已吊销", className: "text-red-400", dot: "revoked" };
  }
  if (!device.enrolled) {
    return device.enrollment_ready
      ? { label: "等待接入", className: "text-amber-400", dot: "pending" }
      : { label: "接入码已过期", className: "text-amber-400", dot: "pending" };
  }
  if (device.online) {
    return { label: "在线", className: "text-emerald-400", dot: "online" };
  }
  return { label: "离线", className: "text-slate-400", dot: "offline" };
}

/** 后端的心跳是 naive UTC（`datetime.utcnow().isoformat()`，不带 Z 也不带偏移）。
    `Date.parse` 对这种串按**本地时区**解释，东八区会凭空多算 8 小时，
    于是「N 秒前」永远走不到，列表里全变成绝对时间。补 Z 才是它们的真实含义。 */
export function parseUtcIso(iso: string): number {
  const withZone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(iso.trim()) ? iso : `${iso.trim()}Z`;
  return Date.parse(withZone);
}

/** 审计行那种「几点几分发生的」：给本地时刻，不能直接切字符串。
    后端串是 naive UTC，`slice(5,16)` 切出来的就是 UTC 的时分，
    老板对不上自己墙上的钟（他也想不起来是时区的问题）。 */
export function formatLocalStamp(iso: string | null | undefined): string {
  if (!iso) return "—";
  const ms = parseUtcIso(iso);
  if (Number.isNaN(ms)) return iso.slice(0, 16);
  const d = new Date(ms);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} `
    + `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** 最近心跳：只说「刚刚/N 分钟前」没意义——sidecar 挂没挂住是运维问题，
    老板需要知道确切时刻才能和「我几点关的电脑」对上。 */
export function formatHeartbeat(iso: string | null | undefined,
                                nowMs: number): string {
  if (!iso) return "从未心跳";
  const seen = parseUtcIso(iso);
  if (Number.isNaN(seen)) return "时间未知";
  const diff = Math.max(0, Math.floor((nowMs - seen) / 1000));
  if (diff < 60) return `${diff} 秒前`;
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  return formatLocalStamp(iso);
}

/** 执行区下拉里的名字：把「能不能用这次操作」前置到选择时，
    而不是等后端返回「设备离线」才发现。 */
export function deviceOptionLabel(device: RemoteDevice, isDefault: boolean): string {
  const base = `${device.name}（${deviceTone(device).label}）`;
  const caps = device.capabilities?.length
    ? ` · ${device.capabilities.length} 项能力` : "";
  return `${isDefault ? "★ " : ""}${base}${caps}`;
}

/** 侧栏启动命令。接入码只在建设备那一刻出现，命令也就只能当场复制一次。 */
export function buildSidecarCommand(opts: {
  server: string;
  code: string;
  allowRoot?: string;
}): string {
  const server = opts.server.replace(/\/+$/, "");
  const root = (opts.allowRoot || "").trim();
  const enroll = `python3 aieos_sidecar.py enroll --server ${server}`
    + ` --code ${opts.code}`
    + (root ? ` --allow-root ~/${root.replace(/^\.?\//, "")}` : "");
  return `${enroll}\npython3 aieos_sidecar.py run`;
}

/** 派发行状态文案。expired 与 failed 必须在 UI 上分得开：
    一个是「设备没回报（多半掉线/卡授权弹窗）」，一个是「设备跑了但失败」。 */
export function dispatchStatusLabel(status: string): string {
  switch (status) {
    case "pending": return "排队中";
    case "claimed": return "执行中";
    case "succeeded": return "成功";
    case "failed": return "设备执行失败";
    case "expired": return "未回报（超时）";
    default: return status;
  }
}

/** 从执行结果 / 审计行的 result JSON 里取出「这次真的在哪台机器上跑的」。
    远程执行才有 device 引用；没有就返回 null（= 后端本机，改造前的老路径）。

    只认 `device.name`：设备 id 贴在日志里没人看得懂，而且名字是老板自己起的。 */
export function remoteDeviceName(payload: unknown): string | null {
  if (!payload || typeof payload !== "object") return null;
  const rec = payload as Record<string, unknown>;
  const candidates = [rec.device, (rec.result as Record<string, unknown> | undefined)?.device];
  for (const item of candidates) {
    if (item && typeof item === "object") {
      const name = (item as Record<string, unknown>).name;
      if (typeof name === "string" && name.trim()) return name.trim();
    }
  }
  return null;
}
