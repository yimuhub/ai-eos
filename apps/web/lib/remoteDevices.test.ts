import { describe, expect, it } from "vitest";

import {
  buildSidecarCommand,
  deviceOptionLabel,
  deviceTone,
  dispatchStatusLabel,
  formatHeartbeat,
  formatLocalStamp,
  remoteDeviceName,
  type RemoteDevice,
} from "./remoteDevices";

const NOW = Date.parse("2026-11-03T10:00:00Z");

function device(over: Partial<RemoteDevice> = {}): RemoteDevice {
  return {
    id: "d1", name: "老张的 MacBook", platform: "darwin", hostname: "mbp-01",
    capabilities: ["run_shell", "read_file", "write_file"],
    enrolled: true, online: true, revoked: false,
    last_seen_at: "2026-11-03T09:59:40Z", enrollment_ready: false, ...over,
  };
}

describe("deviceTone", () => {
  it("在线设备显示在线", () => {
    expect(deviceTone(device()).label).toBe("在线");
  });

  // 吊销的设备服务端仍留着最近一次心跳时间，判序错了就会把黑名单画成绿点
  it("已吊销优先于在线", () => {
    expect(deviceTone(device({ revoked: true })).label).toBe("已吊销");
    expect(deviceTone(device({ revoked: true })).dot).toBe("revoked");
  });

  it("没接入分「等接入码」和「码过期」", () => {
    expect(deviceTone(device({ enrolled: false, enrollment_ready: true })).label)
      .toBe("等待接入");
    expect(deviceTone(device({ enrolled: false, enrollment_ready: false })).label)
      .toBe("接入码已过期");
  });

  it("接入过但没心跳才是离线", () => {
    expect(deviceTone(device({ online: false, last_seen_at: null })).label)
      .toBe("离线");
  });
});

describe("formatHeartbeat", () => {
  it("60 秒内给秒数", () => {
    expect(formatHeartbeat("2026-11-03T09:59:40Z", NOW)).toBe("20 秒前");
  });

  it("一小时内给分钟", () => {
    expect(formatHeartbeat("2026-11-03T09:30:00Z", NOW)).toBe("30 分钟前");
  });

  it("从没心跳与脏数据都说得清", () => {
    expect(formatHeartbeat(null, NOW)).toBe("从未心跳");
    expect(formatHeartbeat("不是时间", NOW)).toBe("时间未知");
  });

  it("超过一小时落到具体时刻（挂了几天的设备说「2 天前」没法定位）", () => {
    const text = formatHeartbeat("2026-10-31T09:00:00Z", NOW);
    expect(text).toMatch(/^10-31 /);
  });

  // 线上真实形状：DateTime 列是 naive UTC，isoformat() 不带 Z 也不带偏移，
  // 早先 Date.parse 按本地时区读它 → 平白多算 8 小时 → 心跳永远显示绝对时刻。
  it("后端给的 naive UTC 也按 UTC 算", () => {
    expect(formatHeartbeat("2026-11-03T09:59:40", NOW)).toBe("20 秒前");
    expect(formatHeartbeat("2026-11-03T09:30:00", NOW)).toBe("30 分钟前");
  });

  it("带微秒的 naive UTC 一样认（utcnow().isoformat() 就长这样）", () => {
    // 差值向下取整：09:59:40.123456 → 19.876 秒 → 「19 秒前」
    expect(formatHeartbeat("2026-11-03T09:59:40.123456", NOW)).toBe("19 秒前");
  });

  it("已经带时区的串不再补 Z（补了会变成「时间未知」）", () => {
    expect(formatHeartbeat("2026-11-03T17:59:40+08:00", NOW)).toBe("20 秒前");
    expect(formatHeartbeat("2026-11-03T09:59:40.123456Z", NOW)).toBe("19 秒前");
  });
});

// 审计行原来是 created_at.slice(5,16)：把后端那串 naive UTC 直接当本地时间贴出来，
// 老板看到的「10:07」和他墙上的钟差一整屏。断言只关心「切出来的不是原始串」。
describe("formatLocalStamp", () => {
  it("按本地时区换算，不是把 UTC 的时分贴上去", () => {
    const text = formatLocalStamp("2026-11-03T09:59:40.123456");
    const asLocal = new Date("2026-11-03T09:59:40.123456Z");
    const pad = (n: number) => String(n).padStart(2, "0");
    expect(text).toBe(`${pad(asLocal.getMonth() + 1)}-${pad(asLocal.getDate())} `
      + `${pad(asLocal.getHours())}:${pad(asLocal.getMinutes())}`);
  });

  it("缺时间与脏数据各给一句人话", () => {
    expect(formatLocalStamp(null)).toBe("—");
    expect(formatLocalStamp("not-a-date")).toBe("not-a-date");
  });
});

describe("deviceOptionLabel", () => {
  it("默认执行机带星标并汇总能力数", () => {
    const label = deviceOptionLabel(device(), true);
    expect(label).toBe("★ 老张的 MacBook（在线） · 3 项能力");
  });

  it("没上报能力的设备不显示能力数", () => {
    expect(deviceOptionLabel(device({ capabilities: [] }), false))
      .toBe("老张的 MacBook（在线）");
  });
});

// 这条命令是「照了就能用」的交付物：漏参数会让 sidecar 变成只读设备，
// 老板要在这台机器上被拒绝一次才发现。
describe("buildSidecarCommand", () => {
  it("拼出接入与启动两条", () => {
    const cmd = buildSidecarCommand({
      server: "http://192.168.1.20:8000/", code: "abc123",
      allowRoot: "work/project",
    });
    expect(cmd).toBe(
      "python3 aieos_sidecar.py enroll --server http://192.168.1.20:8000"
      + " --code abc123 --allow-root ~/work/project\n"
      + "python3 aieos_sidecar.py run");
  });

  it("不给目录就不写 --allow-root（明确是只读设备，而不是造一个假路径）", () => {
    const cmd = buildSidecarCommand({ server: "http://h:8000", code: "x1" });
    expect(cmd).not.toContain("--allow-root");
  });

  it("去掉 ./ 前缀，避免 ~ 展开成 ~./work", () => {
    expect(buildSidecarCommand({ server: "http://h:8000", code: "x",
                                 allowRoot: "./work" }))
      .toContain("--allow-root ~/work");
  });
});

describe("dispatchStatusLabel", () => {
  it("超时报「未回报」而不是笼统的「失败」", () => {
    expect(dispatchStatusLabel("expired")).toBe("未回报（超时）");
    expect(dispatchStatusLabel("failed")).toBe("设备执行失败");
    expect(dispatchStatusLabel("claimed")).toBe("执行中");
  });

  it("未知状态原样透出，不假装成功", () => {
    expect(dispatchStatusLabel("weird")).toBe("weird");
  });
});

// 「在哪台机器上跑的」必须在 UI 上说得出口：本机与远程的差别
// 就是「改了谁的硬盘」，含糊不得。
describe("remoteDeviceName", () => {
  it("本机执行没有 device 引用 → null", () => {
    expect(remoteDeviceName({ mode: "REAL", result: { ok: true } })).toBeNull();
  });

  it("执行响应与审计 result 两种形状都认", () => {
    expect(remoteDeviceName({ device: { name: "老张的 MacBook" } }))
      .toBe("老张的 MacBook");
    expect(remoteDeviceName({ result: { ok: true, device: { name: "小张的笔记本" } } }))
      .toBe("小张的笔记本");
  });

  it("脏数据不炸：非对象、缺 name、空串一律 null", () => {
    expect(remoteDeviceName(null)).toBeNull();
    expect(remoteDeviceName("字符串")).toBeNull();
    expect(remoteDeviceName({ device: { id: "d1" } })).toBeNull();
    expect(remoteDeviceName({ device: { name: "   " } })).toBeNull();
  });
});
