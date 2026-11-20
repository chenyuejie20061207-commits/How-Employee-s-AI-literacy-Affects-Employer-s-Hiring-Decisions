from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from common import CONFIG_DIR, WORK_DIR, load_yaml, normalize_source_url, utc_now


def preflight_source(
    source: dict[str, Any], client: Any, user_agent: str = "ResearchMaterialBot/1.0"
) -> dict[str, Any]:
    key = source.get("key", "")
    result: dict[str, Any] = {
        "key": key,
        "checked_at": utc_now(),
        "public_static": bool(source.get("public_static")),
        "terms_confirmed": False,
        "robots_url": "",
        "robots_status": "not_checked",
        "can_fetch": {},
        "allowed": False,
        "reason": "",
    }
    terms = source.get("terms", {})
    terms_confirmed = terms.get("manual_confirmed") is True and bool(
        str(terms.get("reference", "")).strip()
    )
    result["terms_confirmed"] = terms_confirmed
    if not source.get("public_static"):
        result["reason"] = "来源未声明为公开静态页面"
        return result
    if not terms_confirmed:
        result["reason"] = "条款尚未由人工明确确认"
        return result

    try:
        base_url = normalize_source_url(source.get("base_url", ""), source)
        start_urls = [
            normalize_source_url(url, source) for url in source.get("start_urls", [])
        ]
    except ValueError as error:
        result["reason"] = str(error)
        return result
    if not start_urls:
        result["reason"] = "未配置明确的静态页面 URL"
        return result
    base_host = urlsplit(base_url).hostname
    if any(urlsplit(url).hostname != base_host for url in start_urls):
        result["reason"] = "采集 URL 必须与来源 base_url 同域"
        return result

    parsed = urlsplit(base_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    result["robots_url"] = robots_url
    try:
        response = client.get(robots_url, headers={"User-Agent": user_agent})
    except Exception as error:
        result["robots_status"] = "error"
        result["reason"] = f"robots.txt 获取失败: {type(error).__name__}"
        return result

    result["http_status"] = response.status_code
    if response.status_code == 404:
        result["robots_status"] = "missing"
        result["reason"] = "robots.txt 缺失，不能自动视为许可"
        return result
    if response.status_code in {401, 403}:
        result["robots_status"] = "access_denied"
        result["reason"] = "robots.txt 访问被拒绝"
        return result
    if response.status_code != 200:
        result["robots_status"] = "unavailable"
        result["reason"] = f"robots.txt 返回 HTTP {response.status_code}"
        return result

    robots = RobotFileParser()
    robots.set_url(robots_url)
    robots.parse(response.text.splitlines())
    result["robots_status"] = "ok"
    can_fetch = {url: robots.can_fetch(user_agent, url) for url in start_urls}
    result["can_fetch"] = can_fetch
    if not all(can_fetch.values()):
        result["reason"] = "robots.txt 禁止至少一个目标 URL"
        return result
    result["allowed"] = True
    result["reason"] = "静态公开性、人工条款确认和 robots.txt 检查均通过"
    return result


def run_preflight(config: dict[str, Any], client: Any | None = None) -> dict[str, Any]:
    owns_client = client is None
    active_client = client or httpx.Client(
        timeout=float(config.get("timeout_seconds", 15)),
        follow_redirects=False,
        headers={"User-Agent": config.get("user_agent", "ResearchMaterialBot/1.0")},
    )
    try:
        return {
            source.get("key", ""): preflight_source(
                source, active_client, config.get("user_agent", "ResearchMaterialBot/1.0")
            )
            for source in config.get("sources", [])
            if source.get("enabled")
        }
    finally:
        if owns_client:
            active_client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="检查 robots.txt 和人工条款确认状态")
    parser.add_argument("--config", default=str(CONFIG_DIR / "sources.yaml"))
    parser.add_argument("--output", default=str(WORK_DIR / "preflight_state.json"))
    args = parser.parse_args()
    results = run_preflight(load_yaml(args.config))
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if results and all(item["allowed"] for item in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
