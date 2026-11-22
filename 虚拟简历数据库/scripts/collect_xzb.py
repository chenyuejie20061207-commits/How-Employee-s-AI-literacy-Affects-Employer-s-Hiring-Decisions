"""校招宝公开开放 API 的可复现限量抽样程序。

使用前将三期公开快照放入 work/，并以 work/jd_pool.json 作为断点文件。
程序只访问明确允许的 /api/open/v1/，单线程请求间隔不少于 6 秒；
只保留可匹配到非受限原始链接的记录，不访问原始招聘站点。
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from common import BASE_DIR, is_forbidden_source_url, normalize_url

BASE = BASE_DIR / "work"
BASE.mkdir(exist_ok=True)
SNAPSHOTS = [
    BASE / "xiaozhaobao_jobs_2026-11-01.csv",
    BASE / "xiaozhaobao_jobs_2026-10-01.csv",
    BASE / "xiaozhaobao_jobs_2026-09-03.csv",
]
SEARCH_DIR = BASE / "xzb_search_cache"
DETAIL_DIR = BASE / "xzb_detail_cache"
SEARCH_DIR.mkdir(exist_ok=True)
DETAIL_DIR.mkdir(exist_ok=True)
API = "https://xiaozhaobao.com.cn/api/open/v1/jobs"
MIN_INTERVAL = 6.0
TARGET_PER_STRATUM = 28
CITIES = ["北京", "上海", "广州", "深圳"]
CATEGORY_QUERIES = {
    "IT/技术类": ["软件工程师", "算法工程师", "开发工程师", "测试工程师", "嵌入式", "硬件工程师", "数据工程师", "网络安全", "运维工程师", "电气工程师", "研发工程师", "前端开发", "后端开发", "人工智能", "机器学习"],
    "管理类": ["人力资源", "行政管理", "项目经理", "管理培训生", "供应链", "采购", "财务管理", "审计", "运营管理", "生产管理", "人事专员", "财务专员", "物流管理", "储备干部", "企业管理"],
    "非常规认知型": ["产品经理", "产品设计", "行业研究", "咨询顾问", "投资分析", "风险控制", "市场策划", "品牌", "法务", "内容策划", "新媒体", "用户研究", "运营策划", "编辑", "商务拓展", "战略研究", "科研"],
    "常规型": ["客服", "销售", "文员", "行政助理", "出纳", "柜员", "生产操作", "质量检验", "仓储物流", "资料员", "前台", "售后", "跟单", "订单专员", "操作工", "普工", "配送", "收费员", "业务员", "教务"],
}
PATTERNS = {
    "IT/技术类": re.compile(r"软件|算法|开发工程|程序员|测试工程|运维|网络工程|信息安全|网络安全|嵌入式|数据工程|大数据|云计算|人工智能|机器学习|前端|后端|Java|C\+\+|Python|硬件工程|电子工程|通信工程|芯片|FPGA|电气工程|机械工程|自动化工程|工艺工程|研发工程|技术工程", re.I),
    "管理类": re.compile(r"人力资源|人事|行政管理|行政经理|项目经理|管理培训生|管培生|供应链|采购|财务管理|审计|运营管理|生产管理|质量管理|物流管理|储备干部|企业管理", re.I),
    "非常规认知型": re.compile(r"产品|设计|研究|咨询|投资|分析|风控|市场|品牌|策划|法务|律师|编辑|创作|新媒体|用户运营|内容运营|商务拓展|战略|专利|翻译|教学|教师|科研", re.I),
    "常规型": re.compile(r"客服|客户支持|文员|前台|助理|出纳|收银|柜员|操作员|生产|质检|检验|仓储|物流|资料|录入|销售|业务员|店员|售后|装配|普工|配送|票务|收费|呼叫|跟单|订单|行政专员|教务", re.I),
}
GENERIC = re.compile(r"^(.*)?(校园招聘|校招公告|招聘简章|招聘公告|专场招聘|招聘启事)$")
LAST_REQUEST = 0.0


def normalize(value: str | None) -> str:
    value = unicodedata.normalize("NFKC", value or "").lower()
    value = re.sub(r"[\(（]\s*[jJ]\s*\d{3,}\s*[\)）]", "", value)
    value = re.sub(r"(?:20\d{2}\s*届|校园招聘|校招|秋招|春招)", "", value)
    return re.sub(r"[\s\-—_·•（）()【】\[\],，。./\\:：;；'\"&]+", "", value)


def normalize_company(value: str | None) -> str:
    value = normalize(value)
    return re.sub(r"(?:股份有限公司|有限责任公司|有限公司|集团)$", "", value)


def clean_text(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", value or "").replace("\r", "\n")
    text = re.sub(r"(?i)(联系人|联系电话|联系手机|电子邮箱|投递邮箱|简历投递邮箱|微信|QQ|手机号)\s*[:：]?\s*[^\s|，。;；]+", r"\1：[已脱敏]", text)
    text = re.sub(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", "[已脱敏邮箱]", text, flags=re.I)
    text = re.sub(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)", "[已脱敏电话]", text)
    text = re.sub(r"(?<!\d)0\d{2,3}[- ]?\d{7,8}(?:[- ]?\d{1,5})?(?!\d)", "[已脱敏电话]", text)
    text = re.sub(r"(?i)(?:https?://|www\.)\S+", "[已移除外部链接]", text)
    text = re.sub(r"[ \t\u3000]+", " ", text)
    text = re.sub(r"\s*\|\s*", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def request_json(url: str, cache: Path) -> dict:
    global LAST_REQUEST
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    for attempt in range(3):
        wait = MIN_INTERVAL - (time.monotonic() - LAST_REQUEST)
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(url, headers={"User-Agent": "RUC-Labor-Economics-Student-Research/1.0 (public-open-api; non-commercial)"})
        try:
            with urllib.request.urlopen(req, timeout=40) as response:
                LAST_REQUEST = time.monotonic()
                if response.status in (401, 403):
                    raise RuntimeError(f"访问控制状态 {response.status}，停止来源：{url}")
                data = json.load(response)
            cache.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            return data
        except urllib.error.HTTPError as exc:
            LAST_REQUEST = time.monotonic()
            if exc.code in (401, 403):
                raise RuntimeError(f"访问控制状态 {exc.code}，停止来源") from exc
            if exc.code == 429:
                retry_after = exc.headers.get("Retry-After", "30")
                try:
                    delay = min(max(float(retry_after), MIN_INTERVAL), 120)
                except ValueError:
                    delay = 30
            elif exc.code >= 500:
                delay = 2 ** attempt * 5
            else:
                raise
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            delay = 2 ** attempt * 5
        if attempt == 2:
            raise RuntimeError(f"公开API请求失败（已停止重试）：{url}")
        time.sleep(delay)
    raise AssertionError("unreachable")


def dominant(counter: Counter) -> str:
    entries = [
        (value, count) for value, count in counter.items() if value and value != "未披露"
    ]
    return sorted(entries, key=lambda item: (-item[1], item[0]))[0][0] if entries else ""


def load_snapshot() -> dict[str, dict]:
    lookup = {
        "exact": defaultdict(list),
        "position": defaultdict(list),
        "company_meta": defaultdict(
            lambda: {"industry": Counter(), "company_type": Counter()}
        ),
    }
    seen_urls = set()
    for path in SNAPSHOTS:
        if not path.exists():
            continue
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                source_url = row.get("source_url") or ""
                if not source_url or source_url in seen_urls:
                    continue
                seen_urls.add(source_url)
                company_key = normalize_company(row.get("company"))
                exact_key = (company_key, normalize(row.get("position")))
                lookup["exact"][exact_key].append(row)
                lookup["position"][normalize(row.get("position"))].append(row)
                if row.get("industry"):
                    lookup["company_meta"][company_key]["industry"][row["industry"]] += 1
                if row.get("company_type"):
                    lookup["company_meta"][company_key]["company_type"][row["company_type"]] += 1
    return lookup


def classify(title: str) -> tuple[str | None, str]:
    for category in ["IT/技术类", "管理类", "非常规认知型", "常规型"]:
        match = PATTERNS[category].search(title or "")
        if match:
            return category, match.group(0)
    return None, ""


def match_snapshot(result: dict, lookup: dict, city: str) -> dict | None:
    position_key = normalize(result.get("position"))
    exact_rows = lookup["exact"].get(
        (normalize_company(result.get("company")), position_key), []
    )
    rows = [row for row in exact_rows if city in (row.get("city") or "")]
    if rows:
        return rows[0]
    aliases = {
        normalize_company(result.get("company")),
        normalize_company(result.get("parent_company")),
    }
    # 短别名（如“集团”）容易把同名城同岗位错配到无关企业，不予使用。
    aliases = {alias for alias in aliases if len(alias) >= 4}
    for row in lookup["position"].get(position_key, []):
        if city not in (row.get("city") or ""):
            continue
        snapshot_company = normalize_company(row.get("company"))
        if any(alias in snapshot_company or snapshot_company in alias for alias in aliases):
            return row
    return None


def discover(city: str, category: str, lookup: dict) -> list[dict]:
    candidates: dict[int, dict] = {}
    for keyword in CATEGORY_QUERIES[category]:
        for page in range(1, 4):
            params = urllib.parse.urlencode({"city": city, "q": keyword, "size": 50, "sort": "quality", "include_expired": 1, "page": page})
            cache = SEARCH_DIR / f"{city}_{category.replace('/', '_')}_{normalize(keyword)}_{page}.json"
            payload = request_json(API + "?" + params, cache)
            for result in payload.get("results", []):
                rid = int(result["id"])
                if rid in candidates or not result.get("jd_available"):
                    continue
                if city not in (result.get("city") or ""):
                    continue
                title = result.get("position") or ""
                actual, matched = classify(title)
                if actual != category or GENERIC.fullmatch(title.strip()):
                    continue
                if len(clean_text(result.get("summary"))) < 80:
                    continue
                snap = match_snapshot(result, lookup, city)
                source_url = (snap or {}).get("source_url") or ""
                try:
                    normalize_url(source_url)
                except (ValueError, UnicodeError):
                    continue
                if not snap or is_forbidden_source_url(source_url):
                    continue
                snap = dict(snap)
                company_meta = lookup["company_meta"].get(
                    normalize_company(result.get("company")), {}
                )
                if not snap.get("industry") and company_meta:
                    snap["industry"] = dominant(company_meta["industry"])
                if not snap.get("company_type") and company_meta:
                    snap["company_type"] = dominant(company_meta["company_type"])
                candidates[rid] = {
                    "result": result,
                    "query": keyword,
                    "matched_rule": matched,
                    "snapshot": snap,
                }
            print(f"发现 {city}/{category}/{keyword}/第{page}页: 累计 {len(candidates)}", flush=True)
            if len(candidates) >= 120 or not (payload.get("_meta") or {}).get("has_more"):
                break
        if len(candidates) >= 120:
            break
    return list(candidates.values())


def select_candidates(candidates: list[dict], city: str, category: str) -> list[dict]:
    """详情核验阶段只做多样性排序，不提前执行最终配额上限。"""
    pool = list(candidates)
    selected = []
    domains = Counter()
    companies = Counter()
    while pool and len(selected) < 45:
        pool.sort(
            key=lambda c: (
                domains[
                    c["snapshot"].get("source_domain")
                    or (urllib.parse.urlsplit(c["snapshot"]["source_url"]).hostname or "").lower()
                ],
                companies[c["result"].get("company") or "未披露"],
            )
        )
        cand = pool.pop(0)
        snap = cand["snapshot"]
        domain = snap.get("source_domain") or (
            urllib.parse.urlsplit(snap["source_url"]).hostname or ""
        ).lower()
        company = cand["result"].get("company") or "未披露"
        selected.append(cand)
        domains[domain] += 1
        companies[company] += 1
    return selected


def fetch_valid(
    city: str,
    category: str,
    selected: list[dict],
    existing_rows: list[dict] | None = None,
    all_existing_rows: list[dict] | None = None,
) -> list[dict]:
    existing_rows = existing_rows or []
    target = TARGET_PER_STRATUM - len(existing_rows)
    if target <= 0:
        return []
    # 同一岗位可能在不同城市检索中重复出现，必须在全池层面排除。
    existing_ids = {str(row.get("原始岗位ID")) for row in (all_existing_rows or existing_rows)}
    rows = []
    seen_hash = {
        row.get("文本哈希")
        for row in (all_existing_rows or existing_rows)
        if row.get("文本哈希")
    }
    for cand in selected:
        result = cand["result"]
        rid = int(result["id"])
        if str(rid) in existing_ids:
            continue
        payload = request_json(f"{API}/{rid}", DETAIL_DIR / f"{rid}.json")
        detail = payload.get("result") or {}
        jd = clean_text(detail.get("jd"))
        if not (100 <= len(jd) <= 6000):
            print(f"剔除长度不合格 {rid}: {len(jd)}", flush=True)
            continue
        if not re.search(r"职责|要求|任职|岗位|负责|资格|专业|技能", jd):
            continue
        digest = hashlib.sha256(normalize(jd).encode("utf-8")).hexdigest()
        if digest in seen_hash:
            continue
        seen_hash.add(digest)
        snap = cand["snapshot"] or {}
        stamp = (payload.get("_meta") or {}).get("generated_at") or datetime.now(timezone.utc).isoformat()
        rows.append({
            "素材ID": f"JD-XZB-{rid}",
            "来源类型": "招聘信息聚合站",
            "岗位类型": category,
            "行业": detail.get("industry") or snap.get("industry") or "未披露",
            "城市": city,
            "字段类型": "岗位JD",
            "文本内容": jd,
            "采集时间": stamp,
            "来源记录ID": f"xiaozhaobao:{rid}",
            "来源平台": "校招宝开放API",
            "来源URL": detail.get("url") or result.get("url"),
            "其他来源URL": snap.get("source_url") or "",
            "原始岗位ID": str(rid),
            "采集批次": "xzb-openapi-2026-11-21",
            "原始岗位名称": detail.get("position") or result.get("position") or "",
            "企业名称": detail.get("company") or result.get("company") or "",
            "企业性质": detail.get("company_type") or snap.get("company_type") or "未披露",
            "企业规模": "未披露",
            "分类理由": f"公开API检索词：{cand['query']}；岗位名称优先规则命中：{cand['matched_rule']}",
            "文本哈希": digest,
            "重复组ID": "",
            "脱敏状态": "自动脱敏v1",
            "人工复核状态": "待复核",
            "原始来源域名": snap.get("source_domain") or "未匹配",
            "原始来源平台": snap.get("source") or "未匹配",
            "原始城市": detail.get("city") or result.get("city") or city,
            "原始发布日期": detail.get("publish_date") or result.get("publish_date") or "未披露",
            "学历要求": detail.get("education") or "未披露",
            "招聘批次": detail.get("batch") or "未披露",
        })
        print(f"有效 {city}/{category}: {len(existing_rows) + len(rows)}/{TARGET_PER_STRATUM}（{rid}）", flush=True)
        if len(rows) >= target:
            break
    return rows


def write_pool(rows: list[dict]) -> None:
    path = BASE / "jd_pool.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def main() -> None:
    lookup = load_snapshot()
    all_rows = []
    if (BASE / "jd_pool.json").exists():
        all_rows = json.loads((BASE / "jd_pool.json").read_text(encoding="utf-8"))
    stratum_counts = Counter((r["城市"], r["岗位类型"]) for r in all_rows if r.get("文本内容"))
    completed = {key for key, count in stratum_counts.items() if count >= TARGET_PER_STRATUM}
    for city in CITIES:
        for category in CATEGORY_QUERIES:
            existing_rows = [r for r in all_rows if (r["城市"], r["岗位类型"]) == (city, category)]
            if (city, category) in completed:
                print(f"跳过已完成 {city}/{category}（{len(existing_rows)}条）", flush=True)
                continue
            candidates = discover(city, category, lookup)
            selected = select_candidates(candidates, city, category)
            print(f"待详情核验 {city}/{category}: {len(selected)}；已有{len(existing_rows)}条", flush=True)
            rows = fetch_valid(city, category, selected, existing_rows, all_rows)
            all_rows.extend(rows)
            write_pool(all_rows)
            print(f"分层完成 {city}/{category}: {len(existing_rows) + len(rows)}", flush=True)
    print(f"采集结束，总计 {len(all_rows)} 条有效JD", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"采集中止：{exc}", file=sys.stderr, flush=True)
        raise
