# V5.0 升级方案 · 专家库 × 组织形态（大版本）

> 版本：V4.2 → **V5.0**（大版本号，因涉及新数据模型 + 跨模块能力）
> 主题：专家库补全到 302、专家可编辑/复制/手建、数字员工一键绑定专家、行业组织形态预设一键落地

---

## 一、专家数量补齐（40 → 302）

**根因**：最初 vendor 导入漏抓了子目录，缺 40 个（不是 10 个）：
- 游戏开发系 9：Unity / Unreal / Godot / Roblox / Blender
- 工程深水区 21：RAG / 隐私 / WebAssembly / 支付 / 实时协同 / 搜索相关性…
- 安全 2、测试 1、枢纽战略 1

**做法**：40 个概念简报已入队，`tools/rewrite_experts.py` 后台生成中，
过 10-gram 相似度门（与上游 0 照搬）。生成完跑 `import-original`，专家库到 **302**。

---

## 二、专家库三大操作能力

| 能力 | 后端 | 前端 | 说明 |
|---|---|---|---|
| **编辑专家** | `PUT /experts/{slug}` | 详情页「✏️ 编辑」 | 改 名称/emoji/部门/简介/system_prompt 全文 |
| **复制专家** | `POST /experts/{slug}/copy` | 详情页「📋 复制」 | 基于现有专家生成新 slug，改完即用 |
| **手动新建** | `POST /experts/manual` | 专家库「＋ 手动添加」 | 不依赖 302，凭空新建人设档案 |

**硬规约（用户永久要求）**：手动新建表单**必带 📎 附件口**（复用 `AttachmentPicker.tsx`），
上传能力包/知识库/参考文档。

**数据**：以上三种都写 `expert_templates`，`source="ai-eos-original"`，
手动新建的 `source_slug` 用 `custom-<随机>`，与预设 302 区分。

---

## 三、文案清理（去「原创声明」「AI-EOS 原创版」）

**前端删**：
- 专家库页顶部「📜 专家库原创声明」可折叠横幅 → 删
- 各处「AI-EOS 原创版」「AI-EOS 原创」展示文案 → 删

**保留（合规底裤，不动）**：
- `docs/EXPERTS_STATEMENT.md` 完整声明（给法务/审计看）
- `vendor/.../LICENSE`（MIT 合规）
- 数据库 `source`/`license` 字段（数据层留痕，不展示）

> 原则：**展示层去掉，合规层留下**。商业化时法务要看的是 docs + LICENSE，不是 UI。

---

## 四、数字员工 × 专家库打通（招聘 / 批量添加）

`agents/page.tsx` 的两个入口都加「专家库」联动：

1. **招新员工**（单）
   - 新增「📚 从专家库选择」下拉（按部门分组 + 搜索框，302 可搜）
   - 选中 → 自动回填 姓名 / 岗位 / system_prompt
   - 也可**手动**填（保留现有输入框），两种共存
2. **批量添加**（多）
   - 新增「📚 批量勾选专家库」面板：按部门多选，一键批量生成数字员工
   - 保留现有 textarea 手动输入
   - 三选一：手动输入 / 勾选专家库 / 手动混输

**新增数据字段**：`agents.expert_template_id`（关联 `expert_templates.id`），
agent 激活时若绑定专家，直接取该专家的 `system_prompt` 作为人设。
这就是「配备智能体，直接调用专家库里的专家」。

---

## 五、行业组织形态预设 + 一键引入（核心新能力）

### 5.1 内置行业（最少 8 个，可扩展）

每个预设 = **部门树 + 岗位 + 推荐专家（expert_slug）**，落在 `content/org_presets/*.json`：

| 行业 | 预设 id | 部门示例 |
|---|---|---|
| 互联网/科技公司 | `tech` | 高管·产品·工程·设计·数据·增长 |
| 制造业 | `manufacturing` | 供应链·生产·质量·工艺工程 |
| 零售/电商 | `retail` | 增长营销·运营·客服·库存 |
| 医疗健康 | `healthcare` | 医疗编码·客服·营销合规 |
| 教育培训 | `education` | 课程·招生·学员运营 |
| 金融 | `finance` | 分析·风控·投研·合规 |
| 建筑地产 | `construction` | 土木·项目·工程·物业 |
| 政府/公共事业 | `government` | 数字化·政务·公共服务 |

### 5.2 数据模型（`content/org_presets/tech.json` 示意）

```json
{
  "id": "tech", "industry": "互联网 / 科技公司", "emoji": "💻",
  "departments": [
    { "name": "产品部", "desc": "产品规划与迭代",
      "agents": [
        { "name": "产品经理", "role": "产品规划", "expert_slug": "product-manager" },
        { "name": "增长黑客", "role": "用户增长", "expert_slug": "marketing-growth-hacker" }
      ] },
    { "name": "工程部", "desc": "研发与交付",
      "agents": [
        { "name": "AI 工程师", "role": "LLM 应用", "expert_slug": "engineering-ai-engineer" },
        { "name": "后端架构师", "role": "系统架构", "expert_slug": "engineering-backend-architect" }
      ] }
  ]
}
```

### 5.3 一键引入流程（organization/page.tsx）

```
选行业 → 预览部门树 + 各岗位推荐专家 → [📥 一键引入]
  → POST /organizations/presets/{id}/apply
  → ① 建部门树  ② 按 agents[] 批量建数字员工（绑定 expert_template_id）
  → 员工即「配备好专家的智能体」，可直接调用专家库专家干活
```

**优化点（我在你方案上加的）**：
- 引入前**可预览 + 可勾选**：不是全量强塞，用户能只选部分部门/岗位
- 冲突处理：已存在的部门/员工自动跳过并提示「已存在 N 个，跳过 M 个，新建 K 个」
- 每个行业预设顶部给「一句话定位」，选起来不懵

---

## 六、版本与交付

- 版本号：`package.json` / 登录页 / 侧边栏 / CHANGELOG → **V5.0**
- tag + Release：`v5.0.0`（走 `github-release-workflow` 技能）
- 交付物：专家库 302 + 三大操作 + 文案清理 + 招聘/批量联动 + 8 行业组织形态一键落地

## 七、风险与说明

1. **专家 40 个仍在生成**：V5.0 其余模块可先行开发，最后统一跑 `import-original`。
2. **组织形态是「推荐」非「强制」**：引入后岗位仍可调，专家绑定可换。
3. **手动新建专家**默认 `enabled=true`，但需 `import-original` 之外的独立写路径
   （`POST /experts/manual` 直接落库，不走文件 import）。
