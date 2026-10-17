# -*- coding: utf-8 -*-
"""话术库 CSV 导入导出测试。"""
from __future__ import annotations

import csv
import shutil
from pathlib import Path

from pdd_cs.config import Config, PROJECT_ROOT
from pdd_cs.engine.rules import load_rules
from pdd_cs.rules_io import export_csv, import_csv


def _temp_config(tmp_path: Path) -> Config:
    config = Config.load(PROJECT_ROOT / "config" / "config.example.yaml")
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    mapping = {
        "keyword_file": "keywords.yaml",
        "transfer_file": "transfer.yaml",
        "replace_file": "replace.yaml",
        "filter_file": "filters.yaml",
        "goods_file": "goods.yaml",
        "blacklist_file": "blacklist.yaml",
        "qc_file": "qc.yaml",
    }
    for attr, name in mapping.items():
        source = PROJECT_ROOT / "config" / "rules" / name
        target = rules_dir / name
        if source.exists():
            shutil.copyfile(source, target)
        else:
            target.write_text("[]\n", encoding="utf-8")
        setattr(config.rules, attr, target)
    return config


def _loaded(config: Config):
    return load_rules(
        config.rules.keyword_file,
        config.rules.transfer_file,
        config.rules.replace_file,
        config.rules.filter_file,
        config.rules.goods_file,
        config.rules.blacklist_file,
    )


def test_export_keywords_csv(tmp_path: Path):
    config = _temp_config(tmp_path)
    target = tmp_path / "keywords.csv"
    count = export_csv(config, "keywords", target)
    assert count >= 10
    with target.open("r", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == count
    assert {"id", "keywords", "reply"} <= set(rows[0].keys())
    assert any(row["id"] == "ship_time" for row in rows)


def test_import_keywords_csv(tmp_path: Path):
    config = _temp_config(tmp_path)
    csv_path = tmp_path / "import.csv"
    csv_path.write_text(
        "id,keywords,match,reply,reply_type,priority,enabled\n"
        "custom-1,测试关键词|testkey,fuzzy,这是导入的回复,text,50,1\n"
        "ship_time,什么时候发货|何时发货,fuzzy,更新后的发货回复,text,20,1\n",
        encoding="utf-8-sig",
    )
    result = import_csv(config, "keywords", csv_path)
    assert result["added"] == 1
    assert result["updated"] == 1
    assert result["total"] >= 18

    rules = _loaded(config)
    assert rules.match_keyword("测试关键词").reply == "这是导入的回复"
    assert "更新后的发货回复" in rules.match_keyword("什么时候发货").reply
    # 导出再导入不能把“未写 enabled”的规则误改成停用
    assert sum(1 for rule in rules.keywords if rule.enabled) >= 17


def test_import_replace_mode(tmp_path: Path):
    config = _temp_config(tmp_path)
    csv_path = tmp_path / "replace.csv"
    csv_path.write_text(
        "id,keywords,match,reply,reply_type,priority,enabled\n"
        "only-one,唯一关键词,fuzzy,唯一回复,text,10,1\n",
        encoding="utf-8-sig",
    )
    result = import_csv(config, "keywords", csv_path, replace=True)
    assert result["total"] == 1
    rules = _loaded(config)
    assert rules.stats()["keywords"] == 1
    assert rules.match_keyword("唯一关键词") is not None


def test_export_import_transfer_and_replace(tmp_path: Path):
    config = _temp_config(tmp_path)
    for rule_type in ("transfer", "replace"):
        csv_path = tmp_path / f"{rule_type}.csv"
        count = export_csv(config, rule_type, csv_path)
        assert count >= 1
        result = import_csv(config, rule_type, csv_path)
        assert result["total"] == count
    rules = _loaded(config)
    assert rules.stats()["transfers"] >= 5
    assert rules.stats()["replaces"] >= 5
