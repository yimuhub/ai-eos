// 导航单一事实源（Single Source of Truth）
//
// ## 为什么要独立成文件
// 改造前导航只存在于 `components/Layout.tsx` 的一个数组里，于是有三个问题：
//   1. 侧边栏是唯一的入口——35 个平铺项，新用户无法建立"这个系统能干什么"的心智模型；
//   2. 想做全局搜索/命令面板时，没有地方能拿到"系统里到底有哪些页面"；
//   3. 页面标题、面包屑、最近访问都需要同一份数据，各自维护必然漂移。
//
// 现在侧边栏与命令面板共用这一份定义。每个页面额外带三类信息：
//   - `desc`    一句话说明"这个页面替我解决什么"，命令面板直接展示，也是新用户的教学材料；
//   - `keywords` 检索别名（英文名、拼音首字母、口语说法），让用户按自己的词找到页面；
//   - `hidden`  不进侧边栏、但可被搜索到（如内部测试台），把菜单收敛成"老板真正关心的"。
//
// 新增页面时**只需要改这一个文件**，侧边栏与命令面板自动生效。

export type NavItem = {
  href: string;
  label: string;
  icon: string;
  /** 一句话说明这个页面能做什么（命令面板展示，也是新用户的教学文案） */
  desc: string;
  /** 检索别名：英文、拼音、口语说法。用户不一定知道产品的命名习惯 */
  keywords?: string[];
  /** true = 不在侧边栏出现，但仍可被 ⌘K 搜到 */
  hidden?: boolean;
};

export type NavGroup = {
  /** 分组名，同时作为命令面板的分组标题 */
  title: string;
  /** 分组一句话说明，说明"这一组为什么放在一起" */
  hint?: string;
  items: NavItem[];
};

/** 侧边栏分组：按老板的使用动线排列——先看战况、再管组织、然后干活、沉淀资产、最后复盘 */
export const NAV_GROUPS: NavGroup[] = [
  {
    title: "指挥中枢",
    hint: "每天先看这里：现在在打什么仗、仗打到哪了",
    items: [
      { href: "/", label: "指挥中心", icon: "◈", desc: "全局总览：公司、员工、任务与今日动向", keywords: ["dashboard", "home", "zhihuizhongxin", "首页", "总览", "概览"] },
      { href: "/command", label: "一句话指挥台", icon: "📣", desc: "说一句话，AI 判断派活/专家组队/作战室立项，每步都有你的确认点", keywords: ["command", "zhichutai", "指挥台", "一句话", "下达", "派活", "组队", "立项", "orchestrator"] },
      { href: "/projects", label: "项目中心", icon: "▤", desc: "按项目组织目标：建项目、拉团队、下目标", keywords: ["project", "xiangmu", "项目", "目标"] },
      { href: "/warroom", label: "作战室", icon: "🎯", desc: "盯单个目标的实时执行过程与产出", keywords: ["warroom", "zuozhanshi", "作战", "盯盘"] },
      { href: "/tasks", label: "任务", icon: "◉", desc: "所有任务的统一列表：状态、负责人、产物", keywords: ["task", "renwu", "ren wu", "任务", "待办"] },
      { href: "/autonomous", label: "自治运行", icon: "♾", desc: "让员工自己持续跑：自治等级与紧急停止", keywords: ["autonomous", "zizhi", "自治", "自动运行", "停机"] },
      { href: "/companies", label: "公司管理", icon: "🏢", desc: "建公司、切公司、管理公司基本信息", keywords: ["company", "gongsi", "公司", "多公司", "租户"] },
    ],
  },
  {
    title: "组织与智能体",
    hint: "你的人力资源部：招人、排组织、配模型",
    items: [
      { href: "/agents", label: "Agent 团队", icon: "⬡", desc: "你的数字员工：新建、编辑、暂停、批量管理", keywords: ["agent", "employee", "员工", "yuangong", "智能体", "团队成员"] },
      { href: "/organization", label: "组织架构", icon: "⌗", desc: "部门树与汇报关系，决定谁向谁汇报", keywords: ["organization", "org", "zuzhi", "部门", "架构", "汇报线"] },
      { href: "/factory", label: "Agent 工厂", icon: "✦", desc: "按目标批量生成整套团队，而不是一个个招", keywords: ["factory", "gongchang", "工厂", "批量生成", "建团队"] },
      { href: "/experts", label: "专家库", icon: "📚", desc: "265 位即插即用专家人设：找专家、组队、一键进工厂上岗", keywords: ["expert", "zhuanjia", "专家", "角色库", "agency", "模板", "人设"] },
      { href: "/agent-offboarding", label: "离职交接", icon: "⇥", desc: "员工离职时把记忆、任务、资产交接给接手人", keywords: ["offboarding", "lizhi", "离职", "交接", "辞退"] },
      { href: "/models", label: "模型配置", icon: "◈", desc: "接入哪家大模型、用哪个密钥、谁绑哪个模型", keywords: ["model", "moxing", "模型", "密钥", "API Key", "LLM", "大模型"] },
    ],
  },
  {
    title: "协作执行",
    hint: "员工之间怎么开会、怎么接线、什么时候自动干",
    items: [
      { href: "/meetings", label: "会议中心", icon: "◍", desc: "多个员工开会讨论问题，产出结论", keywords: ["meeting", "huiyi", "会议", "讨论"] },
      { href: "/workflow-builder", label: "工作流", icon: "⇄", desc: "把多步骤流程串起来，按顺序自动执行", keywords: ["workflow", "gongzuoliu", "工作流", "流程", "编排"] },
      { href: "/schedules", label: "日程安排", icon: "🗓", desc: "定时触发：每天/每周固定时间自动开工", keywords: ["schedule", "richeng", "日程", "定时", "cron", "排期"] },
      { href: "/automation", label: "自动化规则", icon: "🤖", desc: "「发生 X 就做 Y」的事件触发规则", keywords: ["automation", "zidonghua", "自动化", "触发器", "规则"] },
      { href: "/device-console", label: "设备操作台", icon: "🖥", desc: "授权员工操作本机：执行命令、读写文件", keywords: ["device", "computer", "shebei", "设备", "本机", "命令行", "shell"] },
    ],
  },
  {
    title: "知识与资产",
    hint: "员工学什么、产出放哪、下次怎么复用",
    items: [
      { href: "/knowledge", label: "知识库", icon: "❑", desc: "上传资料作为员工的事实依据，减少胡编", keywords: ["knowledge", "zhishi", "知识", "文档", "RAG", "资料"] },
      { href: "/skill-center", label: "技能中心", icon: "⚡", desc: "员工会哪些技能、哪些可被学会", keywords: ["skill", "jineng", "技能", "能力"] },
      { href: "/artifacts", label: "成果中心", icon: "▣", desc: "员工交出来的成品：报告、表格、代码", keywords: ["artifact", "chengguo", "成果", "产出", "交付物"] },
      { href: "/delivery", label: "交付中心", icon: "📦", desc: "按批次查看交付物与验收状态", keywords: ["delivery", "jiaofu", "交付", "验收"] },
      { href: "/doc-styles", label: "文档风格", icon: "🎨", desc: "统一文档模板与排版风格，产出不用再返工", keywords: ["style", "template", "fengge", "风格", "排版", "格式"] },
      { href: "/memory", label: "记忆库", icon: "🧠", desc: "员工记住的事实与经验，可查可删", keywords: ["memory", "jiyi", "记忆", "长期记忆"] },
      { href: "/templates", label: "模板库", icon: "📋", desc: "可复用的任务与文档模板", keywords: ["template", "muban", "模板"] },
      { href: "/assets", label: "素材资产", icon: "🖼", desc: "图片等素材的统一管理", keywords: ["asset", "sucai", "素材", "图片", "资产"] },
    ],
  },
  {
    title: "成长与治理",
    hint: "员工有没有进步、有没有越界、钱花在哪",
    items: [
      { href: "/growth-center", label: "成长中心", icon: "🌱", desc: "员工的成长路径与培养动作", keywords: ["growth", "chengzhang", "成长", "培养"] },
      { href: "/reflection", label: "复盘中心", icon: "🔄", desc: "任务结束后的自我复盘与教训沉淀", keywords: ["reflection", "fupan", "复盘", "总结", "教训"] },
      { href: "/training", label: "培训中心", icon: "🎓", desc: "针对性训练员工并对比训练前后效果", keywords: ["training", "peixun", "培训", "训练"] },
      { href: "/performance", label: "绩效中心", icon: "📊", desc: "系统靠不靠谱：成功率、耗时、失败原因，以及谁在拖后腿", keywords: ["performance", "jixiao", "绩效", "考核", "KPI", "可靠性", "成功率", "度量", "metrics", "失败原因", "归因"] },
      { href: "/governance", label: "治理与审计", icon: "⬢", desc: "合规策略与越界行为的审计记录", keywords: ["governance", "zhili", "治理", "合规", "审计"] },
      { href: "/external-agents", label: "外部协作", icon: "🔗", desc: "接入外部 Agent 参与公司业务", keywords: ["external", "waibu", "外部", "第三方", "协作"] },
      { href: "/intervention", label: "干预中心", icon: "🛠", desc: "员工卡住或跑偏时，人工介入并批准", keywords: ["intervention", "ganyu", "干预", "人工", "审批", "介入"] },
      { href: "/skill-growth", label: "技能成长档案", icon: "📈", desc: "技能随任务增长的完整档案", keywords: ["skill growth", "jineng", "成长档案", "技能曲线"] },
      { href: "/token-stats", label: "Token统计", icon: "🪙", desc: "花了多少钱、花在哪个模型上", keywords: ["token", "cost", "billing", "统计", "费用", "成本", "计费"] },
      { href: "/lan-share", label: "局域网共享", icon: "📡", desc: "手机扫二维码接入同一份数据：Web 版直进、App 接管、开机自启", keywords: ["lan", "share", "lajuan", "局域网", "共享", "扫码", "二维码", "手机", "mobile", "二维码", "qrcode", "开机自启", "autostart", "配对", "pairing"] },
      { href: "/diagnostics", label: "系统诊断", icon: "🩺", desc: "一键体检数据库/磁盘/局域网/升级通道，异常项给修复建议", keywords: ["diagnostics", "doctor", "诊断", "体检", "健康"] },
      { href: "/logs", label: "审计日志", icon: "📜", desc: "系统里发生过的每一件事", keywords: ["log", "audit", "日志", "审计", "记录"] },
    ],
  },
];

/** 不进侧边栏、但仍可被 ⌘K 搜到的内部页面。
 *
 * 这些页面要么是调试性质、要么使用频率极低——放进菜单只会增加新用户的负担，
 * 但对熟悉系统的人是刚需。所以「藏起来、搜得到」比「删掉」或「平铺」都更合适。 */
export const HIDDEN_ITEMS: NavItem[] = [
  { href: "/test-console", label: "接口测试台", icon: "🧪", desc: "内部调试用：直接调用后端接口查看原始返回", keywords: ["test", "console", "ceshi", "调试", "接口测试", "debug"], hidden: true },
];

/** 全部可检索页面（侧边栏 + 隐藏页），带所属分组名 */
export const ALL_PAGES: (NavItem & { group: string })[] = [
  ...NAV_GROUPS.flatMap((g) => g.items.map((it) => ({ ...it, group: g.title }))),
  ...HIDDEN_ITEMS.map((it) => ({ ...it, group: "内部工具" })),
];

/** 路径 → 导航项。用于页面标题、面包屑、最近访问的名称回填。
 *
 * 详情页（`/agents/xxx`）没有独立导航项，回退到其父级列表页，
 * 这样标题至少是「Agent 团队」而不是空白。
 */
export function findNavByPath(pathname: string): (NavItem & { group: string }) | null {
  const exact = ALL_PAGES.find((it) => it.href === pathname);
  if (exact) return exact;
  // 详情页回退到最长的前缀匹配（避免 /tasks 抢走 /tasks/xxx 之外的东西）
  const candidates = ALL_PAGES
    .filter((it) => it.href !== "/" && pathname.startsWith(it.href + "/"))
    .sort((a, b) => b.href.length - a.href.length);
  return candidates[0] ?? null;
}

/** 某个导航项是否处于当前路径下。
 *
 * 只用 `pathname === href` 会漏掉所有详情页（`/agents/xxx` 不会高亮
 * "Agent 团队"），用户会失去"我在哪"的感觉。根路径必须精确匹配，
 * 其余用前缀匹配，并避免 `/task` 误匹配 `/tasks`。
 */
export function isNavActive(pathname: string, href: string): boolean {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(href + "/");
}
