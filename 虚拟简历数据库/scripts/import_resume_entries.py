"""虚构简历条目导入通道：批量导入条目 JSONL，拒收任何真实/模板简历痕迹。

与 manual_import.py 的结构通道并存、守卫同样严格：
- RAW_RESUME_MARKERS 命中即拒收；
- PII 零容忍，命中直接拒收，不以掩码代替拒收；
- 模块格式、长度、来源可追溯、禁采域拦截逐条校验。
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from clean import clean_text
from common import (
    AI_ENTRY_PREFIX,
    AI_LEVEL_NA,
    AI_SIGNAL_LEVELS,
    ENTRY_DEIDENTIFICATION_STATUS,
    ENTRY_FIELD_TYPES,
    JOB_TYPE_ABBR,
    MODULE_ABBR,
    normalize_url,
    schema_record,
    sha256_text,
    is_forbidden_source_url,
)
from deidentify import scan_pii
from manual_import import RAW_RESUME_MARKERS, UnsafeResumeInput

TIME_RE = re.compile(r"\d{4}\.\d{2}–\d{4}\.\d{2}")
ENTRY_ID_RE = re.compile(r"^(VRE|VAI)-(IT|MGT|NRC|REG)-(EDU|INT|PRJ|SKL)-(\d{2})$")
ENTRY_MIN, ENTRY_MAX = 30, 400
AI_ENTRY_MIN, AI_ENTRY_MAX = 80, 300
PROFICIENCY_WORDS = ("熟练", "掌握", "了解")
DEGREE_WORDS = ("本科", "硕士", "专科")


def validate_entry_text(field_type: str, text: str) -> str:
    cleaned = clean_text(text)
    for marker in RAW_RESUME_MARKERS:
        if marker in cleaned:
            raise UnsafeResumeInput(f"条目包含真实/原始简历特征词“{marker}”，已拒绝导入")
    if scan_pii(cleaned):
        raise UnsafeResumeInput("条目包含可识别个人信息，已拒绝导入；不会以掩码代替拒绝")
    if not ENTRY_MIN <= len(cleaned) <= ENTRY_MAX:
        raise UnsafeResumeInput(f"条目长度 {len(cleaned)} 超出 {ENTRY_MIN}-{ENTRY_MAX}")
    if "\ufffd" in cleaned:
        raise UnsafeResumeInput("条目含Unicode替换字符")
    if field_type in ("实习经历条目", "项目经历条目"):
        # clean_text 做 NFKC 规范化，全角｜会转为 ASCII |
        if "|" not in cleaned.splitlines()[0]:
            raise UnsafeResumeInput("经历条目首行须为“名称｜角色｜起止时间”")
        if not TIME_RE.search(cleaned):
            raise UnsafeResumeInput("经历条目缺少 YYYY.MM–YYYY.MM 起止时间")
        bullets = [line for line in cleaned.splitlines() if line.startswith("- ")]
        if len(bullets) < 2:
            raise UnsafeResumeInput("经历条目至少包含 2 条要点")
    elif field_type == "教育经历条目":
        if not TIME_RE.search(cleaned):
            raise UnsafeResumeInput("教育条目缺少 YYYY.MM–YYYY.MM 起止时间")
        if not any(word in cleaned for word in DEGREE_WORDS):
            raise UnsafeResumeInput("教育条目缺少学历层次")
    elif field_type == "技能条目":
        if ":" not in cleaned:
            raise UnsafeResumeInput("技能条目须按“技能簇：技能项”组织")
        if not any(word in cleaned for word in PROFICIENCY_WORDS):
            raise UnsafeResumeInput("技能条目缺少熟练度表述")
    return cleaned


def import_entry_record(values: dict[str, Any]) -> dict[str, str]:
    if values.get("来源类型") != "公开简历模板":
        raise UnsafeResumeInput("虚构条目的来源类型必须是公开简历模板")
    field_type = str(values.get("字段类型", ""))
    if field_type not in ENTRY_FIELD_TYPES:
        raise UnsafeResumeInput("字段类型必须是教育经历、实习经历、项目经历或技能条目")
    job_type = str(values.get("岗位类型", ""))
    entry_id = str(values.get("素材ID", "")).strip()
    m = ENTRY_ID_RE.match(entry_id)
    if not m:
        raise UnsafeResumeInput(f"素材ID不符合 VRE-/VAI- 命名规则: {entry_id}")
    family, job_abbr, mod_abbr, seq = m.group(1), m.group(2), m.group(3), m.group(4)
    if JOB_TYPE_ABBR.get(job_type) != job_abbr or MODULE_ABBR.get(field_type) != mod_abbr:
        raise UnsafeResumeInput("素材ID与岗位类型/字段类型不一致")

    ai_level = str(values.get("AI素养信号等级", "")).strip()
    if family == AI_ENTRY_PREFIX:
        if ai_level not in AI_SIGNAL_LEVELS:
            raise UnsafeResumeInput("VAI 条目必须标注 AI素养信号等级 L0–L5")
        if int(seq) != int(ai_level[1:]) + 1:
            raise UnsafeResumeInput("VAI 条目序号与 AI素养信号等级不一致（01=L0 … 06=L5）")
    else:
        if ai_level != AI_LEVEL_NA:
            raise UnsafeResumeInput("VRE 中性条目的 AI素养信号等级必须是“不适用”")

    text = validate_entry_text(field_type, str(values.get("文本内容", "")))
    if family == AI_ENTRY_PREFIX and not AI_ENTRY_MIN <= len(text) <= AI_ENTRY_MAX:
        raise UnsafeResumeInput(f"VAI 条目长度 {len(text)} 超出 {AI_ENTRY_MIN}-{AI_ENTRY_MAX}")

    source_url = normalize_url(str(values.get("来源URL", "")))
    other_url = str(values.get("其他来源URL", "")).strip()
    if other_url:
        other_url = normalize_url(other_url)
    for url in (source_url, other_url):
        if is_forbidden_source_url(url):
            raise UnsafeResumeInput(f"来源属于禁采域: {url}")

    required = ("来源记录ID", "来源平台", "城市", "分类理由", "采集批次")
    missing = [field for field in required if not str(values.get(field, "")).strip()]
    if missing:
        raise UnsafeResumeInput(f"条目来源不可追溯或字段缺失: {'、'.join(missing)}")

    org_name = str(values.get("企业名称", "")).strip()
    if field_type in ("实习经历条目", "项目经历条目"):
        for field in ("企业名称", "行业", "企业性质", "企业规模"):
            if not str(values.get(field, "")).strip() or str(values.get(field)) in ("不适用", "未披露"):
                raise UnsafeResumeInput(f"经历条目必须登记虚构机构属性: {field}")
    else:
        if org_name != "不适用":
            raise UnsafeResumeInput("教育/技能条目的企业名称必须是“不适用”")

    record = schema_record(
        {
            **values,
            "素材ID": entry_id,
            "来源类型": "公开简历模板",
            "字段类型": field_type,
            "文本内容": text,
            "采集时间": values.get("采集时间") or "",
            "来源URL": source_url,
            "其他来源URL": other_url,
            "原始岗位ID": "",
            "原始岗位名称": "",
            "AI素养信号等级": ai_level,
            "文本哈希": sha256_text(text),
            "重复组ID": "",
            "脱敏状态": ENTRY_DEIDENTIFICATION_STATUS,
            "人工复核状态": "自动校验通过（待人工复核）",
        }
    )
    return record


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="导入虚构简历条目；只接受全虚构、可追溯的条目")
    parser.add_argument("--input", required=True, help="条目 JSONL")
    parser.add_argument("--output", required=True, help="输出 CSV")
    parser.add_argument("--base", help="可选：合并进已有正式表 CSV（追加条目行）")
    args = parser.parse_args()

    from export import export_csv, read_csv

    records = [import_entry_record(item) for item in read_jsonl(Path(args.input))]
    ids = [record["素材ID"] for record in records]
    if len(set(ids)) != len(ids):
        raise UnsafeResumeInput("素材ID 存在重复")

    if args.base:
        existing = read_csv(args.base)
        existing_ids = {row["素材ID"] for row in existing}
        overlap = existing_ids.intersection(ids)
        if overlap:
            raise UnsafeResumeInput(f"与既有正式表素材ID重复: {sorted(overlap)[:5]}")
        records = existing + records

    export_csv(records, args.output)
    print(f"已导入条目并写出 {len(records)} 行 -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
