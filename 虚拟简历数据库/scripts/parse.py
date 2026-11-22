from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from clean import clean_text
from common import (
    JOB_TYPES,
    SOURCE_TYPES,
    load_taxonomy,
    load_yaml,
    material_id,
    normalize_source_url,
    schema_record,
    sha256_text,
    utc_now,
)
from deidentify import deidentify_record


def classify_job(
    title: str, text: str, taxonomy: dict[str, Any] | None = None
) -> tuple[str, str]:
    config = taxonomy or load_taxonomy()
    combined = f"{title}\n{text}".casefold()
    for job_type in config.get("classification_priority", JOB_TYPES):
        if job_type not in JOB_TYPES:
            continue
        keywords = config.get("job_keywords", {}).get(job_type, [])
        matched = [keyword for keyword in keywords if keyword.casefold() in combined]
        if matched:
            return job_type, f"按优先级命中{job_type}关键词：{'、'.join(matched)}"
    return "", "未命中分类规则，需人工分类"


def _selected_text(soup: BeautifulSoup, selector: str, attribute: str = "") -> str:
    if not selector:
        return ""
    node = soup.select_one(selector)
    if node is None:
        return ""
    if attribute:
        return clean_text(str(node.get(attribute, "")))
    return clean_text(node.get_text(" ", strip=True))


def parse_jd_html(
    html: str,
    source: dict[str, Any],
    source_url: str,
    collected_at: str | None = None,
    batch: str = "manual",
    taxonomy: dict[str, Any] | None = None,
) -> dict[str, str]:
    if not source.get("public_static"):
        raise ValueError("只解析已声明为公开静态页面的来源")
    source_type = source.get("source_type", "")
    if source_type not in SOURCE_TYPES[:2]:
        raise ValueError("JD 来源类型必须是公开招聘平台或招聘信息聚合站")

    normalized_url = normalize_source_url(source_url, source)
    soup = BeautifulSoup(html, "lxml")
    selectors = source.get("selectors", {})
    jd_text = clean_text(_selected_text(soup, selectors.get("jd", "")))
    title = _selected_text(soup, selectors.get("title", ""))
    if not jd_text:
        raise ValueError(f"页面未提取到岗位 JD: {normalized_url}")
    if not title:
        raise ValueError(f"页面未提取到岗位名称: {normalized_url}")

    attribute = source.get("source_record_id_attribute", "")
    source_record_id = _selected_text(
        soup, selectors.get("source_record_id", ""), attribute
    ) or material_id("SOURCE", normalized_url)
    job_type, reason = classify_job(title, jd_text, taxonomy)
    other_source_url = source.get("other_source_url", "")
    if other_source_url:
        other_source_url = normalize_source_url(other_source_url, source)
    record = schema_record(
        {
            "素材ID": material_id("JD", source.get("key", "source"), source_record_id, normalized_url),
            "来源类型": source_type,
            "岗位类型": job_type,
            "行业": _selected_text(soup, selectors.get("industry", "")),
            "城市": _selected_text(soup, selectors.get("city", "")),
            "字段类型": "岗位JD",
            "文本内容": jd_text,
            "采集时间": collected_at or utc_now(),
            "来源记录ID": source_record_id,
            "来源平台": source.get("name", ""),
            "来源URL": normalized_url,
            "其他来源URL": other_source_url,
            "原始岗位ID": source_record_id,
            "采集批次": batch,
            "原始岗位名称": title,
            "企业名称": _selected_text(soup, selectors.get("company", "")),
            "企业性质": _selected_text(soup, selectors.get("company_nature", "")),
            "企业规模": _selected_text(soup, selectors.get("company_size", "")),
            "分类理由": reason,
            "文本哈希": sha256_text(jd_text),
            "重复组ID": "",
            "脱敏状态": "未脱敏",
            "人工复核状态": "待复核",
        }
    )
    masked, _ = deidentify_record(record)
    masked["文本哈希"] = sha256_text(masked["文本内容"])
    return schema_record(masked)


def main() -> int:
    parser = argparse.ArgumentParser(description="解析本地静态 HTML 文件，不执行网络请求")
    parser.add_argument("--input", required=True, help="HTML 文件或包含 HTML 的目录")
    parser.add_argument("--source-key", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--batch", default="manual")
    parser.add_argument("--config", default=str(Path(__file__).resolve().parents[1] / "config" / "sources.yaml"))
    args = parser.parse_args()

    config = load_yaml(args.config)
    source = next((item for item in config.get("sources", []) if item.get("key") == args.source_key), None)
    if source is None:
        parser.error(f"未找到来源配置: {args.source_key}")

    input_path = Path(args.input)
    files = sorted(input_path.glob("*.html")) if input_path.is_dir() else [input_path]
    if not files:
        parser.error("没有可解析的 .html 文件")
    records = []
    start_urls = source.get("start_urls", [])
    if not start_urls:
        parser.error("来源必须配置与本地 HTML 对应的 start_urls")
    if len(files) > len(start_urls):
        parser.error("每个 HTML 文件都必须有按文件名排序后对应的 start_urls URL")
    for index, path in enumerate(files):
        source_url = start_urls[index]
        records.append(
            parse_jd_html(
                path.read_text(encoding="utf-8"),
                source,
                source_url,
                batch=args.batch,
            )
        )

    from export import export_csv

    export_csv(records, args.output)
    print(f"已解析 {len(records)} 条 JD")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
