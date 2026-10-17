# 来源说明（合规留档）

本目录 `vendor/agency-agents-zh/` 保留上游项目 `jnMetaCode/agency-agents-zh`（MIT License）
的**许可文件与文档**作为合规留档。

## 重要：这些不是 AI-EOS 的专家内容

AI-EOS 专家库（`/experts` 页面、`expert_templates` 数据库）中的**全部专家人设内容**
已由 AI-EOS 团队使用 LLM **独立完整重写**，存放在 [`../../content/experts/`](../../content/experts/)，
通过 `POST /experts/import-original` 载入数据库（`source=ai-eos-original`，`license=AI-EOS 原创`）。

上游 266 份专家 Markdown 原文**未作为内容来源**，仅其"工种分类框架"（行业通用概念）
被参考。原创声明全文见 [`../../docs/EXPERTS_STATEMENT.md`](../../docs/EXPERTS_STATEMENT.md)。

## 保留的文件（合规最小集）

| 文件 | 保留原因 |
|---|---|
| `LICENSE` | MIT 协议要求保留许可与署名 |
| `README.md` / `EXECUTIVE-BRIEF.md` / `QUICKSTART.md` | 上游项目说明，记录参考的"框架"出处 |
| `*.md`（专家原文） | 2026-10-17 已被 `tools/slim_vendor.py` 移至 `vendor/agency-agents-zh-archive/`，
  本目录只保留上述合规最小集 |

> 上游专家原文如需追溯，在 `vendor/agency-agents-zh-archive/` 中（已移出主目录，不进构建/依赖）。
