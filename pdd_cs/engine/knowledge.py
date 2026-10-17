# -*- coding: utf-8 -*-
"""知识库：Markdown 文件检索。

目录结构（config/knowledge/）示例：

    store-policy.md     店铺政策：发货时效、快递、运费险、退换货、发票
    product-faq.md      商品常见问题：尺码、材质、保质期、使用方式
    pdd-rules.md        平台规则口径：仅退款、负体验、售后时效（只答口径，不替平台下结论）

检索方式：字符二元组 + 关键词加权；不依赖分词库，中文短查询也能命中。
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger("pdd_cs.knowledge")

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_SENTENCE_SPLIT = re.compile(r"[。！？!?；;\n]+")


@dataclass
class KnowledgeSnippet:
    doc: str
    title: str
    section: str
    content: str
    score: float = 0.0

    @property
    def label(self) -> str:
        return f"{self.title} / {self.section}" if self.section else self.title

    def to_prompt_block(self, max_chars: int = 500) -> str:
        body = self.content.strip()
        if len(body) > max_chars:
            body = body[: max_chars - 1] + "…"
        return f"【{self.label}】\n{body}"


def _bigrams(text: str) -> set[str]:
    clean = re.sub(r"\s+", "", text or "")
    if len(clean) < 2:
        return {clean} if clean else set()
    return {clean[i : i + 2] for i in range(len(clean) - 1)}


def _query_terms(query: str) -> set[str]:
    """查询词：二元组 + 2 字以上连续中文片段。"""
    terms = set(_bigrams(query))
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z0-9]{2,}", query or ""):
        if len(chunk) <= 4:
            terms.add(chunk)
        else:
            terms.update(chunk[i : i + 2] for i in range(len(chunk) - 1))
    return {t for t in terms if t}


class KnowledgeBase:
    """Markdown 知识库（按二级标题切块 + 字符二元组检索）。"""

    def __init__(self, directory: Path, *, max_chars_per_snippet: int = 500):
        self.directory = Path(directory)
        self.max_chars_per_snippet = max_chars_per_snippet
        self.snippets: list[KnowledgeSnippet] = []
        self._mtimes: dict[Path, float] = {}
        self.reload()

    # ------------------------------------------------------------------
    def reload(self) -> None:
        self.snippets = []
        self._mtimes = {}
        if not self.directory.exists():
            logger.info("知识库目录不存在（已跳过）: %s", self.directory)
            return
        for path in sorted(self.directory.glob("*.md")):
            try:
                self._load_file(path)
            except OSError as exc:
                logger.warning("读取知识库文件失败 %s: %s", path, exc)
        logger.info("知识库加载完成：%d 个片段（%d 个文件）", len(self.snippets), len(self._mtimes))

    def _load_file(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8")
        self._mtimes[path] = path.stat().st_mtime
        doc_title = path.stem
        current_section = ""
        buffer: list[str] = []
        order = 0

        def flush() -> None:
            nonlocal buffer, current_section, order
            content = "\n".join(line for line in buffer if line.strip()).strip()
            if content:
                order += 1
                self.snippets.append(
                    KnowledgeSnippet(
                        doc=path.name,
                        title=doc_title,
                        section=current_section or "概述",
                        content=content,
                    )
                )
            buffer = []

        for line in text.splitlines():
            heading = _HEADING.match(line.strip())
            if heading:
                flush()
                level = len(heading.group(1))
                title = heading.group(2).strip()
                if level == 1:
                    doc_title = title
                    current_section = ""
                else:
                    current_section = title
                continue
            buffer.append(line)
        flush()

    def reload_if_changed(self) -> bool:
        """文件变化时自动重载；返回是否重载。"""
        if not self.directory.exists():
            return False
        changed = False
        files = list(self.directory.glob("*.md"))
        mtimes = {p: p.stat().st_mtime for p in files}
        if mtimes != self._mtimes:
            self.reload()
            changed = True
        return changed

    # ------------------------------------------------------------------
    def search(
        self,
        query: str,
        *,
        top_k: int = 3,
        min_score: float = 1.0,
    ) -> list[KnowledgeSnippet]:
        """检索最相关的知识片段。"""
        terms = _query_terms(query)
        if not terms or not self.snippets:
            return []
        results: list[KnowledgeSnippet] = []
        for snippet in self.snippets:
            content_grams = _bigrams(snippet.content)
            title_grams = _bigrams(snippet.section + snippet.title)
            score = 0.0
            for term in terms:
                if term in title_grams:
                    score += 3.0
                if term in content_grams:
                    score += 1.0
                elif len(term) >= 2 and term in snippet.content:
                    score += 0.5
            if score <= 0:
                continue
            # 长度归一，避免大段文字天然占优
            score = score / math.sqrt(max(20, len(snippet.content)))
            results.append(
                KnowledgeSnippet(
                    doc=snippet.doc,
                    title=snippet.title,
                    section=snippet.section,
                    content=snippet.content,
                    score=round(score * 100, 2),
                )
            )
        results.sort(key=lambda s: s.score, reverse=True)
        filtered = [s for s in results if s.score >= min_score]
        return filtered[: max(1, top_k)]

    def format_for_prompt(self, snippets: Iterable[KnowledgeSnippet]) -> str:
        blocks = [s.to_prompt_block(self.max_chars_per_snippet) for s in snippets]
        return "\n\n".join(blocks)

    def stats(self) -> dict:
        return {
            "files": len(self._mtimes),
            "snippets": len(self.snippets),
            "dir": str(self.directory),
        }


__all__ = ["KnowledgeBase", "KnowledgeSnippet"]
