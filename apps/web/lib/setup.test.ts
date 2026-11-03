// lib/setup.ts 纯函数测试：行业 → 预设模糊匹配、规模 → 部门子集、预览计数、密码强度、
// 安装草稿与「已安装、待续装」标记的序列化。
//
// 这些规则直接决定"安装后会建出什么组织"，且刻意不依赖后端（后端未上线也能验）。
import { describe, expect, it } from "vitest";
import {
  FALLBACK_PRESET_ID,
  SETUP_DRAFT_VERSION,
  departmentSubset,
  matchPresetId,
  modelBindingSummary,
  parseDraft,
  parsePendingInstall,
  passwordStrength,
  presetPool,
  previewCounts,
  serializeDraft,
  type PresetDetail,
  type PresetSummary,
  type SetupDraft,
} from "./setup";

const LIST: PresetSummary[] = [
  { id: "tech", industry: "互联网 / 科技公司", department_count: 4, agent_count: 9 },
  { id: "retail", industry: "零售 / 电商", department_count: 4, agent_count: 6 },
  { id: "manufacturing", industry: "制造业", department_count: 4, agent_count: 5 },
  { id: "construction", industry: "建筑 / 地产", department_count: 3, agent_count: 4 },
  { id: "education", industry: "教育培训", department_count: 3, agent_count: 3 },
  { id: "healthcare", industry: "医疗健康", department_count: 3, agent_count: 3 },
  { id: "pack_content", industry: "一人公司·内容岗位包", department_count: 1, agent_count: 4 },
  { id: "bigco_alibaba", industry: "大厂仿照·阿里巴巴（电商+云AI双核）", department_count: 11, agent_count: 40 },
];

describe("presetPool", () => {
  it("排除 bigco_*（照抄某家公司，不是行业蓝图）", () => {
    expect(presetPool(LIST).some((p) => p.id === "bigco_alibaba")).toBe(false);
  });
  it("只剩 bigco_* 时退回全量，不至于给不出选项", () => {
    const onlyBigco = LIST.filter((p) => p.id.startsWith("bigco_"));
    expect(presetPool(onlyBigco)).toHaveLength(onlyBigco.length);
  });
});

describe("matchPresetId", () => {
  it("互联网/SaaS → tech", () => {
    expect(matchPresetId(LIST, "internet")).toBe("tech");
  });
  it("电商 / 连锁零售 → retail（不被 bigco 的 industry 串抢走）", () => {
    expect(matchPresetId(LIST, "ecommerce")).toBe("retail");
    expect(matchPresetId(LIST, "retail_chain")).toBe("retail");
  });
  it("制造业 / 建筑工程 / 教育培训 / 医疗健康 命中各自预设", () => {
    expect(matchPresetId(LIST, "manufacturing")).toBe("manufacturing");
    expect(matchPresetId(LIST, "construction")).toBe("construction");
    expect(matchPresetId(LIST, "education")).toBe("education");
    expect(matchPresetId(LIST, "healthcare")).toBe("healthcare");
  });
  it("内容自媒体 → 内容岗位包（industry 串含「内容」）", () => {
    expect(matchPresetId(LIST, "content")).toBe("pack_content");
  });
  it("没有行业预设命中时兜底 tech", () => {
    expect(matchPresetId(LIST, "logistics")).toBe(FALLBACK_PRESET_ID);
    expect(matchPresetId(LIST, "hospitality")).toBe(FALLBACK_PRESET_ID);
    expect(matchPresetId(LIST, "agriculture")).toBe(FALLBACK_PRESET_ID);
  });
  it("行业字段大小写不敏感（SaaS）", () => {
    const upper = [{ id: "x", industry: "SAAS 平台", department_count: 1, agent_count: 1 }];
    expect(matchPresetId(upper, "internet")).toBe("x");
  });
  it("列表为空也给出兜底值", () => {
    expect(matchPresetId([], "internet")).toBe(FALLBACK_PRESET_ID);
  });
});

const DETAIL: PresetDetail = {
  id: "tech",
  industry: "互联网 / 科技公司",
  departments: [
    { name: "产品部", agents: [{ name: "a" }, { name: "b" }] },
    { name: "工程部", agents: [{ name: "c" }, { name: "d" }, { name: "e" }, { name: "f" }] },
    { name: "设计部", agents: [{ name: "g" }, { name: "h" }] },
    { name: "运维", agents: [{ name: "i" }] },
  ],
};

describe("departmentSubset（规模 → 部门名，名称全部来自接口返回）", () => {
  it("个体户：不引入任何部门", () => {
    expect(departmentSubset(DETAIL, "solo")).toEqual([]);
  });
  it("工作室：前 2 个", () => {
    expect(departmentSubset(DETAIL, "studio")).toEqual(["产品部", "工程部"]);
  });
  it("中小公司：前 4 个", () => {
    expect(departmentSubset(DETAIL, "smb")).toEqual(["产品部", "工程部", "设计部", "运维"]);
  });
  it("集团大厂：全部", () => {
    expect(departmentSubset(DETAIL, "group")).toHaveLength(4);
  });
  it("预设部门少于规模时不报错、不补名", () => {
    const small: PresetDetail = { id: "p", industry: "x", departments: [{ name: "内容部", agents: [] }] };
    expect(departmentSubset(small, "smb")).toEqual(["内容部"]);
  });
});

describe("previewCounts", () => {
  it("按规模累加对应部门的 AI 数量", () => {
    expect(previewCounts(DETAIL, "studio")).toEqual({
      departments: 2,
      agents: 6,
      names: ["产品部", "工程部"],
    });
    expect(previewCounts(DETAIL, "group")?.agents).toBe(9);
  });
  it("个体户是 0 部门 0 员工", () => {
    expect(previewCounts(DETAIL, "solo")).toEqual({ departments: 0, agents: 0, names: [] });
  });
  it("详情还没拿到时返回 null（界面显示「—」而不是假 0）", () => {
    expect(previewCounts(null, "smb")).toBeNull();
  });
});

describe("passwordStrength（仅装饰，规则以 validate.ts 为准）", () => {
  it("空密码 0 段", () => {
    expect(passwordStrength("")).toBe(0);
  });
  it("太短 0 段", () => {
    expect(passwordStrength("ab1")).toBe(0);
  });
  it("8 位含字母数字：2 段", () => {
    expect(passwordStrength("abcd1234")).toBe(2);
  });
  it("再加长或加符号：3 段", () => {
    expect(passwordStrength("abcd1234!")).toBe(3);
  });
});

const DRAFT: SetupDraft = {
  step: 3,
  username: "boss",
  email: "boss@example.com",
  companyName: "星河文化",
  description: "内容代运营",
  industry: "content",
  orgMode: "preset",
  scale: "studio",
  presetManual: "pack_content",
  model: {
    providerType: "openai_compat",
    name: "Agnes Flash",
    baseUrl: "https://api.agnes-ai.cn/v1",
    modelId: "agnes-2.5-flash",
  },
};

describe("安装草稿：序列化 / 反序列化（可续装）", () => {
  it("白名单字段完整往返", () => {
    expect(parseDraft(serializeDraft(DRAFT))).toEqual(DRAFT);
  });

  it("只持久化白名单：误传的 password / confirm / API Key 不会进字符串", () => {
    const leaky = {
      ...DRAFT,
      password: "SuperSecret123!",
      confirm: "SuperSecret123!",
      apiKey: "sk-live-abcdefgh12345678",
    } as unknown as SetupDraft;
    const raw = serializeDraft(leaky);
    expect(raw).not.toContain("SuperSecret123!");
    expect(raw).not.toContain("sk-live-abcdefgh12345678");
    expect(raw).not.toContain("password");
    expect(raw).not.toContain("apiKey");
    expect(parseDraft(raw)).toEqual(DRAFT);
  });

  it("模型回填字段可用，但不含任何密钥字段", () => {
    const parsed = parseDraft(serializeDraft(DRAFT));
    expect(parsed?.model).toEqual(DRAFT.model);
    expect(Object.keys(parsed?.model ?? {})).toEqual(["providerType", "name", "baseUrl", "modelId"]);
  });

  it("版本不匹配时丢弃", () => {
    expect(parseDraft(JSON.stringify({ ...DRAFT, version: SETUP_DRAFT_VERSION + 1 }))).toBeNull();
    expect(parseDraft(JSON.stringify({ ...DRAFT, version: 0 }))).toBeNull();
    expect(parseDraft(JSON.stringify({ ...DRAFT }))).toBeNull(); // 完全没有 version
  });

  it("非 JSON / 空串 / 非对象 一律当没有草稿", () => {
    expect(parseDraft("not json")).toBeNull();
    expect(parseDraft("")).toBeNull();
    expect(parseDraft(JSON.stringify([DRAFT]))).toBeNull();
    expect(parseDraft(null)).toBeNull();
  });

  it("非法字段被纠正为安全默认值（坏草稿不会把页面搞崩）", () => {
    const parsed = parseDraft(
      JSON.stringify({
        version: SETUP_DRAFT_VERSION,
        step: 99,
        industry: "not-an-industry",
        scale: "huge",
        orgMode: "weird",
        presetManual: 123,
        model: {},
      }),
    );
    expect(parsed).toMatchObject({
      step: 1,
      industry: "internet",
      scale: "smb",
      orgMode: "preset",
      presetManual: null,
      model: null,
    });
  });

  it("第 4 步的 step 能原样往返（是否真的停在 4 由页面结合登录态判定）", () => {
    expect(parseDraft(serializeDraft({ ...DRAFT, step: 4 }))?.step).toBe(4);
  });
});

describe("「已安装、待续装」标记：反序列化", () => {
  const INSTALLED = {
    companyId: "co-1",
    companyName: "拾光科技",
    username: "boss",
    email: "boss@example.com",
    org: { status: "skipped", departments: 0, agents: 0, note: "组织架构尚未生成" },
    model: null,
  };

  const raw = (payload: Record<string, unknown>) =>
    JSON.stringify({ version: SETUP_DRAFT_VERSION, ...payload });

  it("第 2 步刚装完：没有模型、组织未生成，续装要从第 3 步接上", () => {
    expect(parsePendingInstall(raw(INSTALLED))).toEqual({
      companyId: "co-1",
      companyName: "拾光科技",
      username: "boss",
      email: "boss@example.com",
      org: { status: "skipped", departments: 0, agents: 0, note: "组织架构尚未生成" },
      model: null,
    });
  });

  it("第 3 步接好模型后刷新：带上 model，不会把同一条配置再登记一遍", () => {
    const parsed = parsePendingInstall(
      raw({
        ...INSTALLED,
        model: {
          modelId: "m-1",
          modelName: "我的模型",
          modelIdOnProvider: "gpt-5-mini",
          testOk: true,
        },
      }),
    );
    expect(parsed?.model).toEqual({
      modelId: "m-1",
      modelName: "我的模型",
      modelIdOnProvider: "gpt-5-mini",
      testOk: true,
      testError: undefined,
    });
  });

  it("model 里没有配置 id 就当没接过（重试会重新登记，而不是拿空 id 去绑）", () => {
    expect(parsePendingInstall(raw({ ...INSTALLED, model: { modelName: "半成品" } }))?.model).toBeNull();
    expect(parsePendingInstall(raw({ ...INSTALLED, model: "nope" }))?.model).toBeNull();
  });

  it("缺公司 / 组织结果不合法 → 整条丢弃，宁回登录页也不在错的公司上建组织", () => {
    expect(parsePendingInstall(raw({ ...INSTALLED, companyId: "" }))).toBeNull();
    expect(parsePendingInstall(raw({ ...INSTALLED, org: { status: "weird" } }))).toBeNull();
    expect(parsePendingInstall(raw({ ...INSTALLED, org: null }))).toBeNull();
  });

  it("版本不符 / 非 JSON / 空串一律当没有标记", () => {
    expect(parsePendingInstall(raw({ ...INSTALLED, version: 1 }))).toBeNull();
    expect(parsePendingInstall("not json")).toBeNull();
    expect(parsePendingInstall("")).toBeNull();
    expect(parsePendingInstall(null)).toBeNull();
  });

  it("标记里不会出现密钥字段", () => {
    const withSecret = {
      ...INSTALLED,
      apiKey: "sk-live-abcdefgh12345678",
      model: { modelId: "m-1", modelName: "n", modelIdOnProvider: "m", testOk: false, apiKey: "sk-live-abcdefgh12345678" },
    };
    const parsed = parsePendingInstall(raw(withSecret));
    expect(JSON.stringify(parsed)).not.toContain("sk-live-abcdefgh12345678");
    expect(Object.keys(parsed?.model ?? {})).toEqual([
      "modelId", "modelName", "modelIdOnProvider", "testOk", "testError",
    ]);
  });
});

describe("模型绑定情况的一句话措辞", () => {
  it("批量绑定只算新增：跳过的人不能说成「没绑上」", () => {
    expect(modelBindingSummary({ bound: 10, alreadyBound: 0, agentCount: 10 }))
      .toBe("已绑定 10/10 位员工");
    expect(modelBindingSummary({ bound: 6, alreadyBound: 4, agentCount: 10 }))
      .toBe("已绑定 6/10 位员工，4 位已有首选模型未改动");
  });

  it("AI 生成组织落地时已绑好：bound=0 要说「已就绪」，不能写 0/10", () => {
    expect(modelBindingSummary({ bound: 0, alreadyBound: 10, agentCount: 10 }))
      .toBe("10/10 位员工的首选模型已就绪");
    expect(modelBindingSummary({ bound: 0, alreadyBound: 0, agentCount: 10 }))
      .toBe("10 位员工还没绑上首选模型");
    expect(modelBindingSummary({ bound: 0, alreadyBound: 0, agentCount: 0 }))
      .toContain("还没有 AI 员工");
  });
});
