from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Iterable

from common import SCHEMA_FIELDS, schema_record


def export_csv(records: Iterable[dict[str, Any]], path: str | Path) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=SCHEMA_FIELDS,
            quoting=csv.QUOTE_MINIMAL,
            lineterminator="\r\n",
            extrasaction="ignore",
        )
        writer.writeheader()
        for record in records:
            writer.writerow(schema_record(record))


def read_csv(path: str | Path) -> list[dict[str, str]]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != SCHEMA_FIELDS:
            raise ValueError("CSV 表头与规定的 23 字段 schema 不一致")
        return [{field: row.get(field, "") or "" for field in SCHEMA_FIELDS} for row in reader]


def main() -> int:
    parser = argparse.ArgumentParser(description="将 schema JSON 数组导出为 UTF-8 BOM CSV")
    parser.add_argument("--input", required=True, help="JSON 对象数组")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    data = json.loads(Path(args.input).read_text(encoding="utf-8-sig"))
    if not isinstance(data, list):
        parser.error("输入必须是 JSON 对象数组")
    export_csv(data, args.output)
    print(f"已导出 {len(data)} 条记录")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
