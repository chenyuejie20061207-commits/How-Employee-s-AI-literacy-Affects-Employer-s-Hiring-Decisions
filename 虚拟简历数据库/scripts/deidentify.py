from __future__ import annotations

import argparse
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from common import CONFIG_DIR, load_yaml


@lru_cache(maxsize=1)
def default_rules() -> tuple[dict[str, str], ...]:
    config = load_yaml(CONFIG_DIR / "pii_rules.yaml")
    return tuple(config.get("rules", []))


def _rules(rules: Iterable[dict[str, str]] | None) -> tuple[dict[str, str], ...]:
    return tuple(rules) if rules is not None else default_rules()


def scan_pii(text: str, rules: Iterable[dict[str, str]] | None = None) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for rule in _rules(rules):
        for match in re.finditer(rule["pattern"], text or ""):
            findings.append(
                {
                    "type": rule["name"],
                    "start": match.start(),
                    "end": match.end(),
                }
            )
    return sorted(findings, key=lambda item: (item["start"], item["end"], item["type"]))


def mask_pii(
    text: str, rules: Iterable[dict[str, str]] | None = None
) -> tuple[str, list[dict[str, Any]]]:
    findings = scan_pii(text, rules)
    masked = text or ""
    for rule in _rules(rules):
        masked = re.sub(rule["pattern"], rule["replacement"], masked)
    return masked, findings


def deidentify_record(
    record: dict[str, Any], rules: Iterable[dict[str, str]] | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    result = dict(record)
    masked, findings = mask_pii(str(result.get("文本内容", "")), rules)
    result["文本内容"] = masked
    result["脱敏状态"] = "已脱敏"
    result.setdefault("人工复核状态", "待复核")
    if not result.get("人工复核状态"):
        result["人工复核状态"] = "待复核"
    return result, findings


def main() -> int:
    parser = argparse.ArgumentParser(description="扫描并掩码 CSV 文本中的联系人 PII")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--report", help="仅报告位置和类型，不写入 PII 原值")
    args = parser.parse_args()

    from export import export_csv, read_csv

    output_records = []
    reports = []
    for record in read_csv(args.input):
        masked, findings = deidentify_record(record)
        output_records.append(masked)
        if findings:
            reports.append({"素材ID": record.get("素材ID", ""), "findings": findings})
    export_csv(output_records, args.output)
    if args.report:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"records": len(output_records), "with_pii": len(reports)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
