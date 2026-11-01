"""仓库密钥扫描：真实凭据一旦提交，就必须在合并前变红。

## 为什么要"扫描仓库"而不是"检查配置"

.env 被 gitignore 了，但凭据并不只从 .env 泄露：文档示例、脚本默认值、
前端 fixture、`docker-compose.yml` 的环境块都可能是入口。这类问题
代码审查很容易漏（一行 40 个字符的随机串看起来和日志格式没区别），
只有机械扫描能稳定拦住。

## 扫描范围

`git ls-files`（只扫**被跟踪**的文件——没被跟踪的文件进不了仓库，
扫它没有意义还会误报本地临时文件），排除：

* `apps/api/.env`：本地真实密钥的唯一合法位置（已被 .gitignore 拦住，
  此处排除是为了即使有人强制 add 也不在本测试里打印内容）；
* `node_modules/`、`.venv/`、`.next/` 等依赖/构建目录；
* `*.p12`、图片、字体、压缩包等二进制扩展名（另有 NUL 字节探测兜底，
  扩展名写漏的二进制也会被跳过）。

## 白名单纪律

仓库里确实存在**故意的假密钥**（测试夹具、泄露断言的对照值）。它们逐条
列在 `_ALLOWLIST` 里，每条都写明"为什么这不是真实凭据"，且只有
(path, 类型, 值) 三者同时匹配才豁免——不存在整目录豁免。
`test_allowlist_entries_are_not_stale` 会反向校验：一旦夹具被删/被改，
失效的白名单条目会让测试失败，逼着维护者删条目，避免白名单越长越松。

## 失败信息

只给 `文件:行号` 与类型，命中的内容会被打码（保留前 3 个字符），
避免 CI 日志本身变成密钥泄露渠道。
"""
from __future__ import annotations

import pathlib
import re
import subprocess
from dataclasses import dataclass, field

import pytest

# tests/ -> api/ -> apps/ -> 仓库根
REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

# ---------------------------------------------------------------------------
# 规则：只挑"高置信度、低误报"的四类
# ---------------------------------------------------------------------------
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # AWS Access Key ID：AKIA + 16 位大写字母/数字，长度固定，误报极低
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    # OpenAI 风格密钥：sk- 前缀 + 至少 20 位，避免把 sk-learn 之类的词判红
    ("openai_sk", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}")),
    # PEM 私钥头（RSA / EC / OPENSSH / 裸 PEM）
    ("pem_private_key",
     re.compile("-----BEGIN " + r"(RSA |EC |OPENSSH )?" + "PRIVATE KEY-----")),
    # JWT_SECRET 被赋成 32 位以上十六进制串（本项目就是用它签发令牌）
    ("jwt_secret_hex",
     re.compile(r"JWT_SECRET\s*[=:]\s*[\"']?[0-9a-fA-F]{32,}")),
)

# 精确排除（相对仓库根，posix 风格）
_EXCLUDED_FILES = {"apps/api/.env"}
# 目录前缀排除
_EXCLUDED_PREFIXES = (
    "node_modules/",
    ".venv/",
    "apps/api/.venv/",
    "apps/web/.next/",
    "apps/web/node_modules/",
)
# 二进制扩展名排除（另有 NUL 字节探测兜底）
_EXCLUDED_SUFFIXES = {
    ".p12", ".pfx", ".jks", ".keystore",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".pdf", ".zip", ".gz", ".tgz", ".7z", ".dmg", ".exe", ".msi",
    ".wasm", ".so", ".dylib", ".dll", ".jar", ".whl", ".pyc",
    ".db", ".sqlite", ".sqlite3", ".mp3", ".mp4", ".mov",
    ".docx", ".xlsx", ".pptx",
}

# 扫描自身时，允许的"已知假值"常量不写成完整字面量（见文件头说明），
# 这里用拼接，避免扫描器把自己的白名单再命中一遍。
_SK = "sk-"


@dataclass(frozen=True)
class _Allow:
    """一条白名单：必须 (path, kind, value) 三者同时命中才豁免。"""

    path: str
    kind: str
    value: str
    reason: str


_ALLOWLIST: tuple[_Allow, ...] = (
    _Allow(
        path="apps/api/tests/test_model_onboarding.py",
        kind="openai_sk",
        value=_SK + "test-abcdef1234567890",
        reason="测试夹具：sk-test- 前缀 + 顺序字母接连续数字，用于校验写入/脱敏逻辑；"
               "不可能对应任何真实账户",
    ),
    _Allow(
        path="apps/api/tests/test_onboarding_chain.py",
        kind="openai_sk",
        value=_SK + "super-secret-value-should-never-appear",
        reason="测试夹具：值本身写着 should-never-appear，用于断言接口响应与日志里"
               "不会出现密钥明文",
    ),
    _Allow(
        path="apps/web/lib/setup.test.ts",
        kind="openai_sk",
        value=_SK + "live-abcdefgh12345678",
        reason="测试夹具：值是字母表顺序序列 abcdefgh + 12345678，同文件断言它不会被"
               "写进导出文件；live- 只是模拟真实前缀的写法，不是真实密钥",
    ),
)


@dataclass(frozen=True)
class Hit:
    path: str
    line_no: int
    kind: str
    snippet: str  # 已打码的上下文，给失败信息用
    # 原始行：只用于白名单比对，**禁止**出现在断言/日志里（否则等于泄密）
    raw: str = field(repr=False, default="")

    def where(self) -> str:
        return f"{self.path}:{self.line_no} [{self.kind}] {self.snippet}"


def _mask(line: str, match: re.Match[str]) -> str:
    """把命中片段打码，只留前 3 个字符。"""
    secret = match.group(0)
    masked = secret[:3] + f"***({len(secret)} chars)"
    return (line[:match.start()] + masked + line[match.end():]).strip()[:160]


def _tracked_files() -> list[str]:
    """`git ls-files` 的输出（相对仓库根）。非 git 工作区直接跳过。"""
    proc = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        pytest.skip(f"不是 git 工作区，无法枚举被跟踪文件：{proc.stderr.strip()[:200]}")
    return [p for p in proc.stdout.split("\0") if p]


def _eligible(path: str) -> bool:
    if path in _EXCLUDED_FILES:
        return False
    if any(path.startswith(p) for p in _EXCLUDED_PREFIXES):
        return False
    return pathlib.PurePosixPath(path).suffix.lower() not in _EXCLUDED_SUFFIXES


def _read_text(path: pathlib.Path) -> str | None:
    """读文本；二进制（NUL 字节或非 UTF-8）返回 None。"""
    try:
        if not path.is_file():
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw[:4096]:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _scan_files(paths: list[pathlib.Path]) -> list[Hit]:
    hits: list[Hit] = []
    for path in paths:
        text = _read_text(path)
        if text is None:
            continue
        try:
            rel = path.resolve().relative_to(REPO_ROOT).as_posix()
        except ValueError:
            rel = path.as_posix()
        for line_no, line in enumerate(text.splitlines(), start=1):
            for kind, pattern in _PATTERNS:
                m = pattern.search(line)
                if m:
                    hits.append(Hit(rel, line_no, kind, _mask(line, m), raw=line))
    return hits


def _repo_hits() -> list[Hit]:
    return _scan_files([REPO_ROOT / p for p in _tracked_files() if _eligible(p)])


def _allowed(hit: Hit) -> bool:
    return any(
        hit.path == a.path and hit.kind == a.kind and a.value in hit.raw
        for a in _ALLOWLIST
    )


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------

def test_repo_has_no_committed_secrets():
    """被跟踪的文本文件里不得出现真实凭据。"""
    hits = _repo_hits()
    unexpected = [h for h in hits if not _allowed(h)]
    assert not unexpected, (
        "发现疑似真实密钥（已打码，请到对应行确认并轮换/删除后再提交）：\n"
        + "\n".join(h.where() for h in unexpected)
    )


def test_allowlist_entries_are_not_stale():
    """白名单必须"有命中才存在"：夹具改了/删了，条目也得删。"""
    hits = _repo_hits()
    stale = [a for a in _ALLOWLIST if not any(_allowed(h) and h.path == a.path
                                              and h.kind == a.kind for h in hits)]
    assert not stale, "以下白名单条目已不再命中任何内容，请删除（白名单只减不增）：\n" + "\n".join(
        f"{a.path} [{a.kind}] —— {a.reason}" for a in stale
    )


def test_scanner_catches_planted_fake_secrets(tmp_path: pathlib.Path):
    """反向验证：扫描器本身不能"恒绿"。

    用拼接构造样本，避免本文件自己被自己的规则命中。
    """
    sample = tmp_path / "planted.txt"
    sample.write_text(
        "\n".join([
            "aws = " + "AKIA" + "ABCDEFGHIJKLMNOP",
            "openai = " + _SK + ("a" * 32),
            "pem = " + "-----BEGIN " + "RSA " + "PRIVATE " + "KEY-----",
            "jwt = " + "JWT_SECRET=" + ("0" * 40),
            "harmless = sk-learn and token-abc",
        ]),
        encoding="utf-8",
    )
    hits = _scan_files([sample])
    kinds = {h.kind for h in hits}
    assert kinds == {"aws_access_key", "openai_sk", "pem_private_key",
                     "jwt_secret_hex"}, f"漏检：{kinds}"
    assert len(hits) == 4, [h.where() for h in hits]
    # 打码生效：失败信息里不应出现完整密钥
    assert all("ABCDEFGHIJKLMNOP" not in h.snippet for h in hits)


def test_scan_actually_covers_repo():
    """防"空扫描假绿"：规则没错但一个文件都没扫到，同样等于没门禁。"""
    tracked = _tracked_files()
    scanned = [p for p in tracked if _eligible(p)]
    assert len(tracked) > 300, f"被跟踪文件只有 {len(tracked)} 个，git ls-files 可能异常"
    assert len(scanned) > 250, f"实际扫描文件只有 {len(scanned)} 个，排除规则可能过宽"
    assert "apps/api/pyproject.toml" in scanned
    assert "apps/api/.env" not in scanned
