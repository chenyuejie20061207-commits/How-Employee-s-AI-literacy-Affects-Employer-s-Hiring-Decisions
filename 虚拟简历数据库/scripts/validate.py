from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from clean import clean_text
from common import (
    DEIDENTIFICATION_STATUSES,
    ENTRY_DEIDENTIFICATION_STATUS,
    ENTRY_FIELD_TYPES,
    FIELD_TYPES,
    JOB_TYPES,
    REVIEW_STATUSES,
    SCHEMA_FIELDS,
    SOURCE_TYPES,
    STRUCTURE_DEIDENTIFICATION_STATUS,
    STRUCTURE_FIELD_TYPES,
    is_forbidden_source_url,
    load_taxonomy,
    normalize_url,
    sha256_text,
)
from deidentify import scan_pii

CORE_REQUIRED = (
    "素材ID",
    "来源类型",
    "岗位类型",
    "行业",
    "城市",
    "字段类型",
    "文本内容",
    "采集时间",
    "来源记录ID",
    "来源平台",
    "来源URL",
    "采集批次",
    "分类理由",
    "文本哈希",
    "脱敏状态",
    "人工复核状态",
)
JD_REQUIRED = ("原始岗位名称", "企业名称", "企业性质", "企业规模")


def _valid_url(value: str) -> bool:
    try:
        normalize_url(value)
        return True
    except (ValueError, UnicodeError):
        return False


def _valid_time(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.tzinfo is not None
    except (ValueError, AttributeError):
        return False


def _concentration(values: list[str], threshold: float) -> dict[str, Any]:
    counts = Counter(value for value in values if value)
    total = sum(counts.values())
    entries = [
        {
            "value": value,
            "count": count,
            "share": round(count / total, 4) if total else 0.0,
            "exceeds_threshold": bool(total and count / total > threshold),
        }
        for value, count in counts.most_common()
    ]
    return {
        "total_named": total,
        "unique": len(counts),
        "threshold": threshold,
        "entries": entries,
        "max_share": max((item["share"] for item in entries), default=0.0),
    }


def validate_records(
    records: list[dict[str, Any]],
    strict: bool = False,
    taxonomy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = taxonomy or load_taxonomy()
    settings = config.get("validation", {})
    jd_target = int(settings.get("jd_target", 400))
    structure_target = int(settings.get("structure_target", 80))
    entry_target = int(settings.get("entry_target", 80))
    jd_quota = int(settings.get("jd_stratum_quota", 25))
    structure_quota = int(settings.get("structure_cell_quota", 5))
    entry_quota = int(settings.get("entry_cell_quota", 5))
    jd_min, jd_max = settings.get("jd_length", [100, 6000])
    structure_min, structure_max = settings.get("structure_length", [20, 600])
    entry_min, entry_max = settings.get("entry_length", [30, 400])

    errors: list[dict[str, str]] = []

    def error(code: str, record: dict[str, Any], field: str = "", detail: str = "") -> None:
        errors.append(
            {
                "code": code,
                "素材ID": str(record.get("素材ID", "")),
                "field": field,
                "detail": detail,
            }
        )

    jd_strata = {
        f"{job_type}|{city}": {"count": 0, "target": jd_quota}
        for job_type in JOB_TYPES
        for city in config.get("jd_cities", ["北京", "上海", "广州", "深圳"])
    }
    structure_matrix = {
        f"{job_type}|{field_type}": {"count": 0, "target": structure_quota}
        for job_type in JOB_TYPES
        for field_type in STRUCTURE_FIELD_TYPES
    }
    entry_matrix = {
        f"{job_type}|{field_type}": {"count": 0, "target": entry_quota}
        for job_type in JOB_TYPES
        for field_type in ENTRY_FIELD_TYPES
    }
    counts = Counter()
    ids: dict[str, int] = {}
    hashes: dict[str, int] = {}
    domains: list[str] = []
    companies: list[str] = []

    for row_number, record in enumerate(records, start=2):
        material_id = str(record.get("素材ID", ""))
        counts["total"] += 1
        if material_id:
            ids[material_id] = ids.get(material_id, 0) + 1
        for field in CORE_REQUIRED:
            if not str(record.get(field, "") or "").strip():
                error("missing_required", record, field)
        field_type = str(record.get("字段类型", ""))
        is_jd = field_type == "岗位JD"
        is_structure = field_type in STRUCTURE_FIELD_TYPES
        is_entry = field_type in ENTRY_FIELD_TYPES
        if is_jd:
            counts["jd"] += 1
            for field in JD_REQUIRED:
                if not str(record.get(field, "") or "").strip():
                    error("missing_required", record, field)
        elif is_structure:
            counts["structures"] += 1
        elif is_entry:
            counts["entries"] += 1

        if record.get("来源类型") not in SOURCE_TYPES:
            error("invalid_enum", record, "来源类型")
        if record.get("岗位类型") not in JOB_TYPES:
            error("invalid_enum", record, "岗位类型")
        if field_type not in FIELD_TYPES:
            error("invalid_enum", record, "字段类型")
        deidentification_status = record.get("脱敏状态")
        if is_jd:
            family_allowed = ("已脱敏", "未脱敏")
        elif is_structure:
            family_allowed = (STRUCTURE_DEIDENTIFICATION_STATUS, "未脱敏")
        elif is_entry:
            family_allowed = (ENTRY_DEIDENTIFICATION_STATUS, "未脱敏")
        else:
            family_allowed = DEIDENTIFICATION_STATUSES
        if deidentification_status not in DEIDENTIFICATION_STATUSES or (
            deidentification_status not in family_allowed
        ):
            error("invalid_enum", record, "脱敏状态")
        if record.get("人工复核状态") not in REVIEW_STATUSES:
            error("invalid_enum", record, "人工复核状态")
        if is_jd and record.get("来源类型") == "公开简历模板":
            error("source_field_mismatch", record, "来源类型")
        if (is_structure or is_entry) and record.get("来源类型") != "公开简历模板":
            error("source_field_mismatch", record, "来源类型")

        city = str(record.get("城市", "")).strip()
        stratum = f"{record.get('岗位类型', '')}|{city}"
        if is_jd:
            if stratum in jd_strata:
                jd_strata[stratum]["count"] += 1
            else:
                error("unknown_stratum", record, "城市", city)
        if is_structure:
            cell = f"{record.get('岗位类型', '')}|{field_type}"
            if cell in structure_matrix:
                structure_matrix[cell]["count"] += 1
        if is_entry:
            cell = f"{record.get('岗位类型', '')}|{field_type}"
            if cell in entry_matrix:
                entry_matrix[cell]["count"] += 1

        text = clean_text(str(record.get("文本内容", "")))
        if is_jd:
            minimum, maximum = jd_min, jd_max
        elif is_structure:
            minimum, maximum = structure_min, structure_max
        else:
            minimum, maximum = entry_min, entry_max
        if is_jd or is_structure or is_entry:
            if not minimum <= len(text) <= maximum:
                error(
                    "invalid_text_length",
                    record,
                    "文本内容",
                    f"长度 {len(text)}，要求 {minimum}-{maximum}",
                )
            if "\ufffd" in text:
                error("encoding_corruption", record, "文本内容", "含Unicode替换字符")
            findings = scan_pii(text)
            if findings:
                error(
                    "pii_residual",
                    record,
                    "文本内容",
                    "、".join(sorted({finding["type"] for finding in findings})),
                )
            digest = sha256_text(text)
            hashes[digest] = hashes.get(digest, 0) + 1
            if record.get("文本哈希") != digest:
                error("hash_mismatch", record, "文本哈希")
        if record.get("重复组ID"):
            error("unresolved_duplicate_group", record, "重复组ID")

        source_url = str(record.get("来源URL", ""))
        if is_forbidden_source_url(source_url):
            error("forbidden_source", record, "来源URL", "来源协议或robots明确排除")
        if source_url and not _valid_url(source_url):
            error("invalid_url", record, "来源URL")
        elif source_url and is_jd:
            domains.append(urlsplit(source_url).hostname or "")
        other_url = str(record.get("其他来源URL", ""))
        if is_forbidden_source_url(other_url):
            error("forbidden_source", record, "其他来源URL", "来源协议或robots明确排除")
        if other_url and not _valid_url(other_url):
            error("invalid_url", record, "其他来源URL")
        if record.get("采集时间") and not _valid_time(str(record.get("采集时间", ""))):
            error("invalid_timestamp", record, "采集时间")
        if is_jd and record.get("企业名称"):
            companies.append(str(record.get("企业名称")))

        if strict and record.get("人工复核状态") != "已通过":
            error("review_not_passed", record, "人工复核状态")
        if is_jd:
            canonical_status = "已脱敏"
        elif is_structure:
            canonical_status = STRUCTURE_DEIDENTIFICATION_STATUS
        elif is_entry:
            canonical_status = ENTRY_DEIDENTIFICATION_STATUS
        else:
            canonical_status = "已脱敏"
        if strict and deidentification_status != canonical_status:
            error("not_deidentified", record, "脱敏状态")

    for value, count in ids.items():
        if count > 1:
            error("duplicate_id", {"素材ID": value}, "素材ID", f"出现 {count} 次")
    for value, count in hashes.items():
        if count > 1:
            error("duplicate_hash", {"素材ID": ""}, "文本哈希", f"{value[:12]} 出现 {count} 次")

    gaps: list[dict[str, Any]] = []
    if counts["jd"] < jd_target:
        gaps.append({"type": "jd_total", "actual": counts["jd"], "target": jd_target})
    if counts["structures"] < structure_target:
        gaps.append(
            {"type": "structure_total", "actual": counts["structures"], "target": structure_target}
        )
    if counts["entries"] < entry_target:
        gaps.append({"type": "entry_total", "actual": counts["entries"], "target": entry_target})
    for key, item in jd_strata.items():
        item["met"] = item["count"] >= item["target"]
        if not item["met"]:
            gaps.append({"type": "jd_stratum", "key": key, **item})
    for key, item in structure_matrix.items():
        item["met"] = item["count"] >= item["target"]
        if not item["met"]:
            gaps.append({"type": "structure_cell", "key": key, **item})
    for key, item in entry_matrix.items():
        item["met"] = item["count"] >= item["target"]
        if not item["met"]:
            gaps.append({"type": "entry_cell", "key": key, **item})

    domain_report = _concentration(domains, float(settings.get("max_domain_share", 0.5)))
    company_report = _concentration(companies, float(settings.get("max_company_share", 0.1)))
    if strict:
        for kind, concentration in (("domain", domain_report), ("company", company_report)):
            for item in concentration["entries"]:
                if item["exceeds_threshold"]:
                    error(
                        "concentration_exceeded",
                        {"素材ID": ""},
                        kind,
                        f"{item['value']} 占比 {item['share']:.2%}",
                    )
        for gap in gaps:
            errors.append(
                {
                    "code": "target_gap",
                    "素材ID": "",
                    "field": gap["type"],
                    "detail": json.dumps(gap, ensure_ascii=False),
                }
            )

    return {
        "strict": strict,
        "passed": not errors,
        "target_met": not gaps,
        "counts": {
            "total": counts["total"],
            "jd": counts["jd"],
            "structures": counts["structures"],
            "entries": counts["entries"],
        },
        "targets": {"jd": jd_target, "structures": structure_target, "entries": entry_target},
        "jd_strata": jd_strata,
        "structure_matrix": structure_matrix,
        "entry_matrix": entry_matrix,
        "concentration": {"domains": domain_report, "companies": company_report},
        "gaps": gaps,
        "errors": errors,
        "error_counts": dict(Counter(item["code"] for item in errors)),
        "row_count_note": "CSV 数据首行对应第 2 行；报告使用素材ID定位",
    }


def format_text(report: dict[str, Any]) -> str:
    counts = report["counts"]
    targets = report["targets"]
    lines = [
        f"质量检查: {'通过' if report['passed'] else '未通过'}（严格模式：{report['strict']}）",
        f"数量: JD {counts['jd']}/{targets['jd']}，结构 {counts['structures']}/{targets['structures']}，条目 {counts['entries']}/{targets['entries']}，总计 {counts['total']}",
        f"目标配额: {'已满足' if report['target_met'] else '存在缺口'}",
        f"JD 分层: {len(report['jd_strata'])} 个；结构矩阵: {len(report['structure_matrix'])} 格；条目矩阵: {len(report['entry_matrix'])} 格",
        f"域名最大占比: {report['concentration']['domains']['max_share']:.2%}",
        f"企业最大占比: {report['concentration']['companies']['max_share']:.2%}",
        f"缺口数: {len(report['gaps'])}；错误数: {len(report['errors'])}",
    ]
    if report["gaps"]:
        lines.append("缺口明细（不会伪造通过）:")
        lines.extend(f"- {json.dumps(gap, ensure_ascii=False)}" for gap in report["gaps"])
    if report["errors"]:
        lines.append("错误明细:")
        lines.extend(f"- {json.dumps(error, ensure_ascii=False)}" for error in report["errors"])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="输出素材质量报告；严格模式用于最终验收")
    parser.add_argument("--input", required=True)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()

    from export import read_csv

    report = validate_records(read_csv(args.input), strict=args.strict)
    content = (
        json.dumps(report, ensure_ascii=False, indent=2)
        if args.format == "json"
        else format_text(report)
    )
    if args.output:
        Path(args.output).write_text(content + "\n", encoding="utf-8")
    print(content)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
