from __future__ import annotations

import argparse
import json
import re
import unicodedata
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

from common import ENTRY_FIELD_TYPES, SCHEMA_FIELDS, sha256_text


def clean_text(text: str) -> str:
    value = unicodedata.normalize("NFKC", text or "")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = "".join(char if char in "\n\t" or unicodedata.category(char) != "Cc" else " " for char in value)
    value = value.replace("\u00a0", " ").replace("\u3000", " ")
    value = re.sub(r"[ \t]+", " ", value)
    value = "\n".join(line.strip() for line in value.split("\n"))
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def deduplicate_records(
    records: list[dict[str, Any]], threshold: int = 92
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    if not 0 <= threshold <= 100:
        raise ValueError("近似重复阈值必须在 0 到 100 之间")

    unique: list[dict[str, str]] = []
    exact_removed: list[dict[str, str]] = []
    by_hash: dict[str, dict[str, str]] = {}
    for original in records:
        record = {field: str(original.get(field, "") or "") for field in SCHEMA_FIELDS}
        record["文本内容"] = clean_text(record["文本内容"])
        record["文本哈希"] = sha256_text(record["文本内容"])
        digest = record["文本哈希"]
        if digest in by_hash:
            kept = by_hash[digest]
            group_id = f"EXACT-{digest[:12]}"
            kept["重复组ID"] = group_id
            exact_removed.append(
                {
                    "kept_id": kept["素材ID"],
                    "removed_id": record["素材ID"],
                    "group_id": group_id,
                }
            )
            continue
        by_hash[digest] = record
        unique.append(record)

    parent = list(range(len(unique)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    candidates: list[dict[str, Any]] = []
    candidate_indices: set[int] = set()
    scanned_types = ("岗位JD",) + ENTRY_FIELD_TYPES
    scan_indices = [
        index for index, record in enumerate(unique) if record["字段类型"] in scanned_types
    ]
    for offset, left in enumerate(scan_indices):
        left_text = re.sub(r"\s+", "", unique[left]["文本内容"])
        for right in scan_indices[offset + 1 :]:
            right_text = re.sub(r"\s+", "", unique[right]["文本内容"])
            score = fuzz.ratio(left_text, right_text)
            if score >= threshold:
                union(left, right)
                candidate_indices.update((left, right))
                candidates.append(
                    {
                        "left_id": unique[left]["素材ID"],
                        "right_id": unique[right]["素材ID"],
                        "score": round(score, 2),
                    }
                )

    groups: dict[int, list[int]] = {}
    for index in candidate_indices:
        groups.setdefault(find(index), []).append(index)

    id_to_group: dict[str, str] = {}
    for indices in groups.values():
        indices = sorted(set(indices))
        group_seed = "|".join(sorted(unique[index]["素材ID"] for index in indices))
        group_id = f"NEAR-{sha256_text(group_seed)[:12]}"
        for index in indices:
            unique[index]["重复组ID"] = group_id
            id_to_group[unique[index]["素材ID"]] = group_id
    for candidate in candidates:
        candidate["group_id"] = id_to_group[candidate["left_id"]]

    report = {
        "input_count": len(records),
        "output_count": len(unique),
        "exact_removed": exact_removed,
        "near_duplicate_candidates": candidates,
    }
    return unique, report


def main() -> int:
    parser = argparse.ArgumentParser(description="清洗、精确去重并标记 JD 与虚构条目近似重复候选")
    parser.add_argument("--input", required=True, help="输入 CSV")
    parser.add_argument("--output", required=True, help="输出 CSV")
    parser.add_argument("--report", help="可选去重报告 JSON")
    parser.add_argument("--threshold", type=int, default=92)
    args = parser.parse_args()

    from export import export_csv, read_csv

    records, report = deduplicate_records(read_csv(args.input), args.threshold)
    export_csv(records, args.output)
    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
