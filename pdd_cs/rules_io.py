# -*- coding: utf-8 -*-
"""话术库 CSV 导入 / 导出。

日常运营更习惯用 Excel 维护话术。本模块支持三类规则的 CSV 互转：

    keywords  关键词回复
    transfer  转人工关键词
    replace   出站替换词

CSV 使用 UTF-8 BOM 编码（Excel 直接双击不乱码）。导入时按 id / 关键词去重：
存在则更新，不存在则追加；``--replace`` 可整表替换。
"""
from __future__ import annotations

import csv
import io
import logging
from pathlib import Path
from typing import Any

import yaml

from .config import Config

logger = logging.getLogger("pdd_cs.rules_io")

RULE_TYPES = ("keywords", "transfer", "replace")

_HEADERS = {
    "keywords": ["id", "keywords", "match", "reply", "reply_type", "priority", "enabled"],
    "transfer": ["keywords", "reason", "match", "enabled"],
    "replace": ["pattern", "replace", "match", "enabled"],
}


def _path_for(config: Config, rule_type: str) -> Path:
    mapping = {
        "keywords": config.rules.keyword_file,
        "transfer": config.rules.transfer_file,
        "replace": config.rules.replace_file,
    }
    if rule_type not in mapping:
        raise ValueError(f"不支持的规则类型: {rule_type}（可选 {', '.join(RULE_TYPES)}）")
    return mapping[rule_type]


def _read_yaml(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"读取规则文件失败 {path}: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError(f"规则文件格式错误（应为列表）: {path}")
    return [item for item in data if isinstance(item, dict)]


def _write_yaml(path: Path, items: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = "# 由 pdd-cs rules import 自动生成，可继续手工编辑\n"
    path.write_text(header + yaml.safe_dump(items, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _bool_text(value: Any) -> str:
    return "1" if bool(value) else "0"


def _parse_bool(value: Any, default: bool = True) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "y", "是", "启用"}


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------
def export_csv(config: Config, rule_type: str, output: Path) -> int:
    """把规则导出为 CSV，返回行数。"""
    items = _read_yaml(_path_for(config, rule_type))
    output.parent.mkdir(parents=True, exist_ok=True)
    headers = _HEADERS[rule_type]
    with output.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=headers)
        writer.writeheader()
        for item in items:
            row: dict[str, Any] = {}
            for header in headers:
                value = item.get(header, "")
                if header == "enabled":
                    # 规则里没写 enabled 时默认是启用，导出必须继承这个默认值
                    value = _bool_text(item.get("enabled", True))
                elif header == "priority":
                    value = item.get("priority", 100)
                row[header] = value
            writer.writerow(row)
    return len(items)


# ---------------------------------------------------------------------------
# 导入
# ---------------------------------------------------------------------------
def _normalize_row(row: dict[str, str], rule_type: str) -> dict[str, Any] | None:
    row = {str(k).strip(): (v if isinstance(v, str) else "") for k, v in row.items() if k}
    if rule_type == "keywords":
        keywords = (row.get("keywords") or row.get("keyword") or "").strip()
        reply = (row.get("reply") or "").strip()
        if not keywords or not reply:
            return None
        item: dict[str, Any] = {
            "id": (row.get("id") or "").strip() or None,
            "keywords": keywords,
            "match": (row.get("match") or "fuzzy").strip() or "fuzzy",
            "reply": reply,
            "reply_type": (row.get("reply_type") or "text").strip() or "text",
            "priority": int(float(row.get("priority") or 100)),
            "enabled": _parse_bool(row.get("enabled")),
        }
        if not item["id"]:
            item.pop("id")
        return item
    if rule_type == "transfer":
        keywords = (row.get("keywords") or row.get("keyword") or "").strip()
        if not keywords:
            return None
        return {
            "keywords": keywords,
            "reason": (row.get("reason") or "关键词转人工").strip() or "关键词转人工",
            "match": (row.get("match") or "fuzzy").strip() or "fuzzy",
            "enabled": _parse_bool(row.get("enabled")),
        }
    pattern = (row.get("pattern") or row.get("keyword") or "").strip()
    if not pattern:
        return None
    return {
        "pattern": pattern,
        "replace": (row.get("replace") or "").strip(),
        "match": (row.get("match") or "fuzzy").strip() or "fuzzy",
        "enabled": _parse_bool(row.get("enabled")),
    }


def import_csv(
    config: Config, rule_type: str, input_path: Path, *, replace: bool = False
) -> dict[str, Any]:
    """导入 CSV 到规则文件，返回统计。"""
    path = _path_for(config, rule_type)
    raw = input_path.read_text(encoding="utf-8-sig")
    reader = csv.DictReader(io.StringIO(raw))
    rows = list(reader)
    existing = [] if replace else _read_yaml(path)
    key_field = {"keywords": "id", "transfer": "keywords", "replace": "pattern"}[rule_type]

    index: dict[str, int] = {}
    for position, item in enumerate(existing):
        key = str(item.get(key_field) or "").strip()
        if key:
            index[key] = position

    added = updated = skipped = 0
    for row in rows:
        item = _normalize_row(row, rule_type)
        if item is None:
            skipped += 1
            continue
        key = str(item.get(key_field) or "").strip()
        if key and key in index:
            existing[index[key]] = item
            updated += 1
        else:
            existing.append(item)
            added += 1
            if key:
                index[key] = len(existing) - 1

    _write_yaml(path, existing)
    logger.info("规则导入完成：新增 %d，更新 %d，跳过 %d", added, updated, skipped)
    return {"added": added, "updated": updated, "skipped": skipped, "total": len(existing)}


def rule_type_choices() -> tuple[str, ...]:
    return RULE_TYPES


__all__ = ["export_csv", "import_csv", "RULE_TYPES", "rule_type_choices"]
