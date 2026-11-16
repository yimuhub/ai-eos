"""插件 Manifest v1：基座插件的单一描述格式（V6 M2）。

一个包 = 一份 manifest（JSON，存 MarketplacePackage.manifest）+ 发布者信息。
安装时由 `package_installer` 按 category×runtime 分发到真实产物。

六类插件（对齐 docs/升级方案_万能基座V6 §4.2，前四种市场可直接装）：
* tool              —— 工具（mcp_http / mcp_stdio / sidecar_process）
* connector         —— 数据连接器（mcp_http 桥或 declarative 配置）
* workflow_template —— 工作流模板（declarative：图直接内嵌 manifest）
* model_provider    —— 模型供应商（declarative：无密钥登记，密钥走 BYOK）
* skill_pack        —— 技能包（inprocess_python：磁盘插件加载器负责真身，
                        市场装的是"引用+权限"，不是代码分发——无签名机制
                        前不发布可执行代码，这是刻意的安全边界）
* industry_pack     —— 行业包（declarative，M4 启用安装器）

旧四类（AGENT/SKILL/WORKFLOW/DEPARTMENT_TEMPLATE）保留兼容：照旧"仅记账"，
install_log 里如实标注 stub，不冒充真安装。
"""
from __future__ import annotations

import re

_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{2,63}$")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$")

CATEGORIES = ("tool", "connector", "workflow_template", "model_provider",
              "skill_pack", "industry_pack")
LEGACY_CATEGORIES = ("AGENT", "SKILL", "WORKFLOW", "DEPARTMENT_TEMPLATE")
RUNTIMES = ("inprocess_python", "sidecar_process", "mcp_stdio", "mcp_http",
            "declarative")

# category → 允许的 runtime
_RUNTIME_MATRIX = {
    "tool": ("mcp_http", "mcp_stdio", "sidecar_process"),
    "connector": ("mcp_http", "declarative"),
    "workflow_template": ("declarative",),
    "model_provider": ("declarative",),
    "skill_pack": ("inprocess_python",),
    "industry_pack": ("declarative",),
}

# 权限命名空间：network:<域名> / secret:<键名> / data:<域>:<rw> / shell / computer
_PERMISSION_RE = re.compile(
    r"^(network:[a-z0-9.-]+|secret:[a-z0-9_-]+|data:[a-z0-9_-]+:(r|rw|w)|shell|computer)$")
# 含这些权限的包风险自动升 high（宁可吓到用户也不淡化）
_HIGH_RISK_PERMISSIONS = ("shell", "computer")


class ManifestError(ValueError):
    """manifest 校验失败（调用方转 400）。"""


def validate_manifest(manifest: dict | None) -> dict:
    """校验并归一化 manifest；返回归一化后的 dict（原样可入库）。"""
    if not isinstance(manifest, dict):
        raise ManifestError("manifest 必须是对象")
    for field in ("id", "name", "version", "category", "runtime",
                  "description"):
        if not str(manifest.get(field) or "").strip():
            raise ManifestError(f"manifest 缺少必填字段 {field}")
    if not _ID_RE.match(manifest["id"]):
        raise ManifestError("manifest.id 须为 3~64 位小写字母开头，"
                            "只含 a-z 0-9 _ -")
    if not _VERSION_RE.match(manifest["version"]):
        raise ManifestError("manifest.version 须为语义化版本 x.y.z")
    category = manifest["category"]
    runtime = manifest["runtime"]
    if category not in CATEGORIES:
        raise ManifestError(
            f"category 须为 {'/'.join(CATEGORIES)}（旧四类仅兼容登记）")
    if runtime not in RUNTIMES:
        raise ManifestError(f"runtime 须为 {'/'.join(RUNTIMES)}")
    if runtime not in _RUNTIME_MATRIX[category]:
        raise ManifestError(
            f"{category} 类插件的 runtime 只支持 "
            f"{'/'.join(_RUNTIME_MATRIX[category])}")

    risk = manifest.get("risk") or "low"
    if risk not in ("low", "medium", "high"):
        raise ManifestError("risk 须为 low/medium/high")
    permissions = manifest.get("permissions") or []
    if not isinstance(permissions, list):
        raise ManifestError("permissions 必须是数组")
    for perm in permissions:
        if not _PERMISSION_RE.match(str(perm)):
            raise ManifestError(
                f"权限 {perm!r} 不符合命名空间：network:<域名> / secret:<键名>"
                " / data:<域>:<r|rw|w> / shell / computer")
    if any(str(p) in _HIGH_RISK_PERMISSIONS or
           str(p).startswith(("shell", "computer")) for p in permissions):
        risk = "high"

    entry = manifest.get("entry") or {}
    if not isinstance(entry, dict):
        raise ManifestError("entry 必须是对象")
    if category == "tool" and runtime == "mcp_http" \
            and not str(entry.get("endpoint") or "").strip():
        raise ManifestError("tool/mcp_http 需要 entry.endpoint")
    if category == "tool" and runtime == "mcp_stdio" \
            and not str(entry.get("command") or "").strip():
        raise ManifestError("tool/mcp_stdio 需要 entry.command")
    if category == "workflow_template":
        wf = manifest.get("workflow") or {}
        if not isinstance(wf.get("nodes"), list) \
                or not isinstance(wf.get("edges"), list):
            raise ManifestError("workflow_template 需要 workflow.nodes/edges")
    if category == "model_provider":
        prov = entry.get("provider") or {}
        for field in ("provider_type", "name"):
            if not str(prov.get(field) or "").strip():
                raise ManifestError(
                    "model_provider 需要 entry.provider.{provider_type,name}")

    normalized = dict(manifest)
    normalized["risk"] = risk
    normalized["permissions"] = [str(p) for p in permissions]
    normalized["entry"] = entry
    return normalized


def package_key_to_prefix(key: str) -> str:
    """包 key → 工具名前缀（slug 化，保证工具名规则可满足）。"""
    prefix = re.sub(r"[^a-z0-9_-]+", "_", (key or "").lower()).strip("_")
    return prefix or "pkg"
