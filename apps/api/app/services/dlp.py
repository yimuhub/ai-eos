"""出站 DLP 闸门（V6 M5）：工具结果/工作流 HTTP 响应里的 PII 自动脱敏。

设计（docs/升级方案 §4.6 S5）：数据出系统先过闸门——模型经工具拿到的
手机号/身份证/银行卡号默认打码后进上下文，防止「员工把客户手机号写进
周报/发出去」。识别是正则级的（宁可误伤也不漏），命中情况进审计
（tool.call payload.dlp_findings）。

关闭方式：公司功能开关 dlp:outbound 置 disabled（明配置，别静默）。
这层是**出站面**的兜底；入库面的字段级加密归 secret_vault 扩展。
"""
from __future__ import annotations

import re
from typing import Any

# 中国大陆手机号（宽容边界：前后非数字）
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
# 身份证（18 位，末位可 X）
_ID_CARD_RE = re.compile(r"(?<![0-9Xx])[1-9]\d{5}(?:19|20)\d{2}"
                         r"(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])"
                         r"\d{3}[0-9Xx](?![0-9Xx])")
# 银行卡（13-19 位数字，前后非数字；宽松匹配，误伤时宁枉勿纵）
_BANK_RE = re.compile(r"(?<!\d)\d{13,19}(?!\d)")
# 座机（区号-号码）
_TEL_RE = re.compile(r"(?<!\d)0\d{2,3}-\d{7,8}(?!\d)")

TYPES = {"phone": "手机号", "id_card": "身份证", "bank": "银行卡号",
         "tel": "座机"}


def mask_value(kind: str, value: str) -> str:
    if kind == "phone":
        return f"{value[:3]}****{value[-4:]}"
    if kind == "id_card":
        return f"{value[:3]}***********{value[-2:]}"
    if kind == "bank":
        return f"****{value[-4:]}"
    if kind == "tel":
        return f"{value.split('-')[0]}-*******"
    return "****"


def scan_text(text: str) -> list[dict]:
    """扫描一段文本里的 PII，返回 [{type, count, masked_sample}]。"""
    if not text:
        return []
    findings: list[dict] = []
    spans: list[tuple[str, str]] = []
    for m in _ID_CARD_RE.finditer(text):
        spans.append(("id_card", m.group(0)))
    text_wo_ids = _ID_CARD_RE.sub("", text)
    for m in _PHONE_RE.finditer(text_wo_ids):
        spans.append(("phone", m.group(0)))
    for m in _TEL_RE.finditer(text_wo_ids):
        spans.append(("tel", m.group(0)))
    # 银行卡在去掉身份证/手机号后再扫，避免同一串重复计数
    stripped = _PHONE_RE.sub("", _ID_CARD_RE.sub("", text))
    for m in _BANK_RE.finditer(stripped):
        spans.append(("bank", m.group(0)))
    counts: dict[str, int] = {}
    samples: dict[str, str] = {}
    for kind, value in spans:
        counts[kind] = counts.get(kind, 0) + 1
        samples.setdefault(kind, mask_value(kind, value))
    for kind, count in counts.items():
        findings.append({"type": kind, "label": TYPES.get(kind, kind),
                         "count": count, "sample": samples[kind]})
    return findings


def mask_text(text: str) -> tuple[str, list[dict]]:
    """脱敏一段文本，返回 (脱敏后文本, 命中清单)。"""
    if not text:
        return text, []
    findings = scan_text(text)
    if not findings:
        return text, []
    result = text
    result = _ID_CARD_RE.sub(lambda m: mask_value("id_card", m.group(0)),
                             result)
    result = _PHONE_RE.sub(lambda m: mask_value("phone", m.group(0)), result)
    result = _TEL_RE.sub(lambda m: mask_value("tel", m.group(0)), result)
    result = _BANK_RE.sub(lambda m: mask_value("bank", m.group(0)), result)
    return result, findings


def mask_result(result: Any, _depth: int = 0) -> tuple[Any, list[dict]]:
    """递归脱敏工具结果（dict/list/str），命中情况汇总。"""
    if _depth > 6:
        return result, []
    if isinstance(result, str):
        masked, findings = mask_text(result)
        return masked, findings
    if isinstance(result, dict):
        all_findings: list[dict] = []
        masked = {}
        for k, v in result.items():
            mv, f = mask_result(v, _depth + 1)
            masked[k] = mv
            all_findings.extend(f)
        return masked, all_findings
    if isinstance(result, list):
        all_f: list[dict] = []
        masked_list = []
        for item in result:
            mv, f = mask_result(item, _depth + 1)
            masked_list.append(mv)
            all_f.extend(f)
        return masked_list, all_f
    return result, []
