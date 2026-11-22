from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import yaml

BASE_DIR = Path(__file__).resolve().parents[1]
CONFIG_DIR = BASE_DIR / "config"
WORK_DIR = BASE_DIR / "work"

SCHEMA_FIELDS = (
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
    "其他来源URL",
    "原始岗位ID",
    "采集批次",
    "原始岗位名称",
    "企业名称",
    "企业性质",
    "企业规模",
    "分类理由",
    "文本哈希",
    "重复组ID",
    "脱敏状态",
    "人工复核状态",
)
SOURCE_TYPES = ("公开招聘平台", "招聘信息聚合站", "公开简历模板")
JOB_TYPES = ("IT/技术类", "管理类", "非常规认知型", "常规型")
STRUCTURE_FIELD_TYPES = ("教育结构", "实习结构", "项目结构", "技能结构")
FIELD_TYPES = ("岗位JD",) + STRUCTURE_FIELD_TYPES
STRUCTURE_DEIDENTIFICATION_STATUS = "结构抽象v1（不含原文）"
DEIDENTIFICATION_STATUSES = ("已脱敏", "未脱敏", STRUCTURE_DEIDENTIFICATION_STATUS)
REVIEW_STATUSES = (
    "待复核",
    "自动校验通过（待人工复核）",
    "已通过",
    "已驳回",
)
FORBIDDEN_SOURCE_DOMAINS = ("ncss.cn", "gaoxiaojob.com", "yzu.91job.org.cn")


def is_forbidden_source_url(url: str) -> bool:
    """返回URL是否属于协议或robots明确排除的来源，包含全部子域。"""
    try:
        host = (urlsplit(url or "").hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return any(host == domain or host.endswith("." + domain) for domain in FORBIDDEN_SOURCE_DOMAINS)

TRACKING_PARAMETERS = {
    "from",
    "ref",
    "referrer",
    "source",
    "spm",
    "track",
    "tracking",
    "_ga",
    "gclid",
    "fbclid",
}


def load_yaml(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def load_taxonomy() -> dict[str, Any]:
    return load_yaml(CONFIG_DIR / "taxonomy.yaml")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def material_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def normalize_url(url: str, preserve_params: Iterable[str] | None = None) -> str:
    preserved = {key.casefold() for key in (preserve_params or ())}
    value = (url or "").strip()
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"仅支持有效的 HTTP(S) URL: {url!r}")
    if parsed.username or parsed.password:
        raise ValueError("URL 不得包含用户名或密码")

    host = parsed.hostname.encode("idna").decode("ascii").lower()
    if parsed.port and not (
        (parsed.scheme.lower() == "http" and parsed.port == 80)
        or (parsed.scheme.lower() == "https" and parsed.port == 443)
    ):
        host = f"{host}:{parsed.port}"

    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query_items = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        if (
            lowered.startswith("utm_") or lowered in TRACKING_PARAMETERS
        ) and lowered not in preserved:
            continue
        query_items.append((key, item))
    query = urlencode(sorted(query_items), doseq=True)
    return urlunsplit((parsed.scheme.lower(), host, path, query, ""))


def normalize_source_url(url: str, source: dict[str, Any]) -> str:
    # 某些来源（如校招宝）的条款要求保留 ref 归因参数，不能按普通跟踪参数删除。
    return normalize_url(url, source.get("preserve_query_params", []))


def city_tier(city: str, taxonomy: dict[str, Any] | None = None) -> str:
    config = taxonomy or load_taxonomy()
    stripped = (city or "").strip()
    tiers = config.get("city_tiers", {})
    for tier, cities in tiers.items():
        if stripped in cities:
            return tier
    if stripped and "其他城市" in tiers:
        return "其他城市"
    return "未分层"


def empty_record() -> dict[str, str]:
    return {field: "" for field in SCHEMA_FIELDS}


def schema_record(values: dict[str, Any]) -> dict[str, str]:
    record = empty_record()
    for field in SCHEMA_FIELDS:
        value = values.get(field, "")
        record[field] = "" if value is None else str(value).strip()
    return record
