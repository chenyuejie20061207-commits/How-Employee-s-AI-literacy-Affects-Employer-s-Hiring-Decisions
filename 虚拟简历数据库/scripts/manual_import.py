from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from clean import clean_text
from common import (
    FIELD_TYPES,
    JOB_TYPES,
    STRUCTURE_DEIDENTIFICATION_STATUS,
    material_id,
    normalize_url,
    schema_record,
    sha256_text,
    utc_now,
)
from deidentify import deidentify_record, scan_pii

ABSTRACT_PREFIXES = ("抽象结构:", "结构描述:")
ABSTRACT_TOKENS = (
    "学历层次",
    "专业类别",
    "课程模块",
    "时间段",
    "职责类别",
    "任务类型",
    "成果指标",
    "技能类别",
    "熟练度等级",
    "字段顺序",
)
RAW_RESUME_MARKERS = (
    "个人简历",
    "求职简历",
    "自我评价",
    "出生年月",
    "籍贯",
    "民族",
    "政治面貌",
    "手机号码",
    "联系电话",
    "电子邮箱",
    "家庭住址",
    "身份证号",
    "期望薪资",
)


class UnsafeResumeInput(ValueError):
    pass


def validate_abstract_structure(text: str) -> str:
    cleaned = clean_text(text)
    if not cleaned.startswith(ABSTRACT_PREFIXES):
        raise UnsafeResumeInput("只接受以“抽象结构：”或“结构描述：”开头的抽象描述")
    if not any(token in cleaned for token in ABSTRACT_TOKENS):
        raise UnsafeResumeInput("结构描述必须包含抽象字段，不接受模板原文")
    if any(marker in cleaned for marker in RAW_RESUME_MARKERS):
        raise UnsafeResumeInput("输入包含真实/原始简历特征，已拒绝导入")
    if scan_pii(cleaned):
        raise UnsafeResumeInput("输入包含可识别个人信息，已拒绝导入；不会以掩码代替拒绝")
    return cleaned


def import_structure_record(
    values: dict[str, Any], collected_at: str | None = None, batch: str = "manual-structure"
) -> dict[str, str]:
    if values.get("来源类型") != "公开简历模板":
        raise UnsafeResumeInput("结构素材的来源类型必须是公开简历模板")
    field_type = values.get("字段类型", "")
    if field_type not in FIELD_TYPES[1:]:
        raise UnsafeResumeInput("字段类型必须是教育、实习、项目或技能结构")
    if values.get("岗位类型") not in JOB_TYPES:
        raise UnsafeResumeInput("岗位类型无效")
    text = validate_abstract_structure(str(values.get("文本内容", "")))

    source_url = normalize_url(str(values.get("来源URL", "")))
    other_url = str(values.get("其他来源URL", "")).strip()
    if other_url:
        other_url = normalize_url(other_url)
    required = ("来源记录ID", "来源平台", "行业", "城市", "分类理由")
    missing = [field for field in required if not str(values.get(field, "")).strip()]
    if missing:
        raise UnsafeResumeInput(f"结构来源不可追溯或字段缺失: {'、'.join(missing)}")

    source_record_id = str(values["来源记录ID"]).strip()
    record = schema_record(
        {
            **values,
            "素材ID": material_id("STRUCT", source_url, source_record_id, field_type, text),
            "字段类型": field_type,
            "文本内容": text,
            "采集时间": values.get("采集时间") or collected_at or utc_now(),
            "来源URL": source_url,
            "其他来源URL": other_url,
            "采集批次": values.get("采集批次") or batch,
            "文本哈希": sha256_text(text),
            "重复组ID": "",
            "脱敏状态": "未脱敏",
            "人工复核状态": values.get("人工复核状态") or "待复核",
        }
    )
    result, _ = deidentify_record(record)
    result["脱敏状态"] = STRUCTURE_DEIDENTIFICATION_STATUS
    return schema_record(result)


def _read_input(path: Path) -> list[dict[str, Any]]:
    content = path.read_text(encoding="utf-8-sig").strip()
    if not content:
        return []
    if path.suffix.lower() == ".jsonl":
        return [json.loads(line) for line in content.splitlines() if line.strip()]
    data = json.loads(content)
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise UnsafeResumeInput("JSON 输入必须是对象或对象数组")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description="仅导入抽象化、来源可追溯的简历结构描述")
    parser.add_argument("--input", required=True, help="JSON 或 JSONL；不会保存输入原文")
    parser.add_argument("--output", required=True)
    parser.add_argument("--batch", default="manual-structure")
    args = parser.parse_args()

    records = [import_structure_record(item, batch=args.batch) for item in _read_input(Path(args.input))]
    from export import export_csv

    export_csv(records, args.output)
    print(f"已导入 {len(records)} 条抽象结构；未保存任何真实简历或模板原文")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
