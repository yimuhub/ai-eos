"""本地可解释的语义重排（B5 当前落点）。

没有可用模型 Key 时，用纯标准库实现一条**可解释、可回退**的检索链路：

* 中文友好分词：ASCII 字母数字连续串整体成词；CJK 连续串切成 2-gram
  （单字查询退化为单字）；
* 停用词过滤：去掉「的/了/是/……」这类不携带检索意图的高频字；
* 领域同义词扩展：客户↔用户↔顾客 等十组，查询词权重 1.0，同义词 0.6；
* BM25 打分（k1=1.5, b=0.75）：长度归一化，避免长文档靠词频堆分。

设计上刻意保持无状态、无网络、无第三方依赖，候选文档由调用方传入
``(title, text)`` 列表，``rank`` 只返回 ``(原顺序下标, 分数)``，因此
租户隔离与 SQL 过滤仍由调用方（``company_search``）负责，本模块不接触
数据库，也就没有越权面。

TODO(B5 后续)：接入真实 embedding / 向量库后，本模块保留为离线兜底
（无 Key 或向量服务不可用时降级），接口保持不变。
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Iterable, Sequence

# ASCII 字母数字串与 CJK 连续串交替扫描，保留文本中的先后顺序。
_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u3400-\u4dbf\u4e00-\u9fff]+")

# 单字停用词：只在 token 恰好等于该字时过滤。2-gram 不拆字过滤，
# 否则「和平」会被去掉「和」而变成毫无意义的「平」。
STOPWORDS = frozenset("的了是在和与及等对为把被我你他她它")

# 领域同义词组（组内双向等价）。ASCII 词一律小写，与 tokenize 的输出对齐。
_SYNONYM_GROUPS: tuple[tuple[str, ...], ...] = (
    ("客户", "用户", "顾客"),
    ("收入", "营收", "回款"),
    ("需求", "prd", "功能"),
    ("招聘", "入职", "员工"),
    ("合同", "协议"),
    ("预算", "成本"),
    ("获客", "增长"),
    ("故障", "事故"),
    ("交付", "验收"),
    ("任务", "待办"),
)

SYNONYMS: dict[str, tuple[str, ...]] = {}
for _group in _SYNONYM_GROUPS:
    for _word in _group:
        SYNONYMS[_word] = tuple(w for w in _group if w != _word)
del _group, _word

BM25_K1 = 1.5
BM25_B = 0.75


def tokenize(text: str | None) -> list[str]:
    """把文本切成检索 token；保留重复，供 BM25 统计词频。

    * 全部小写；
    * ``abc123`` 这类 ASCII 字母数字连续串是一个 token；
    * CJK 连续串生成 2-gram；整段只有一个汉字时保留该单字；
    * 单字停用词丢弃；空文本返回 ``[]``。
    """
    if not text:
        return []
    lowered = str(text).lower()
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(lowered):
        chunk = match.group(0)
        if chunk[0].isascii():
            tokens.append(chunk)
            continue
        if len(chunk) == 1:
            tokens.append(chunk)
        else:
            tokens.extend(chunk[i:i + 2] for i in range(len(chunk) - 1))
    return [token for token in tokens if token not in STOPWORDS]


def expand_query(tokens: Iterable[str]) -> dict[str, float]:
    """查询扩展：原词权重 1.0，同义词 0.6；同一词取最高权重。

    返回 dict 的插入顺序即「原词优先、同义词随后」，便于调试时阅读。
    """
    weights: dict[str, float] = {}
    for raw_token in tokens:
        token = str(raw_token).lower()
        if not token:
            continue
        if weights.get(token, 0.0) < 1.0:
            weights[token] = 1.0
        for synonym in SYNONYMS.get(token, ()):
            if weights.get(synonym, 0.0) < 0.6:
                weights[synonym] = 0.6
    return weights


def _document_tokens(
        docs: Sequence[tuple[str, str]],
) -> tuple[list[list[str]], float]:
    """分词并计算平均文档长度；全空文档时平均长度取 1，避免除零。"""
    tokenized: list[list[str]] = []
    total = 0
    for title, text in docs:
        tokens = tokenize(f"{title or ''} {text or ''}")
        tokenized.append(tokens)
        total += len(tokens)
    average = total / len(docs) if docs and total else 1.0
    return tokenized, average


def rank(query: str, docs: Sequence[tuple[str, str]]) -> list[tuple[int, float]]:
    """用 BM25 对候选文档重排，返回 ``[(原下标, 分数)]``。

    * 只返回 ``score > 0`` 的文档（无关项不占名额）；
    * 分数降序，同分按传入顺序；
    * 空查询、空文档、空正文都安全返回 ``[]`` 或跳过，不抛异常。
    """
    if not docs:
        return []
    query_weights = expand_query(tokenize(query))
    if not query_weights:
        return []

    doc_tokens, average_length = _document_tokens(docs)
    doc_count = len(doc_tokens)

    # 文档频率只统计查询（含同义词）里出现过的词；df=0 的查询词不会贡献分数。
    document_frequency: Counter[str] = Counter()
    for tokens in doc_tokens:
        for term in set(tokens):
            if term in query_weights:
                document_frequency[term] += 1

    scored: list[tuple[int, float]] = []
    for index, tokens in enumerate(doc_tokens):
        if not tokens:
            continue
        frequencies = Counter(tokens)
        length = len(tokens)
        score = 0.0
        for term, query_weight in query_weights.items():
            term_frequency = frequencies.get(term, 0)
            if not term_frequency:
                continue
            frequency = document_frequency.get(term, 0)
            if not frequency:
                continue
            # 加 1 的 IDF 始终为正，避免常见词出现负分后被误过滤。
            idf = math.log(
                1.0 + (doc_count - frequency + 0.5) / (frequency + 0.5))
            denominator = term_frequency + BM25_K1 * (
                1.0 - BM25_B + BM25_B * length / average_length)
            score += (query_weight * idf
                      * term_frequency * (BM25_K1 + 1.0) / denominator)
        if score > 0.0:
            scored.append((index, score))

    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    return scored
