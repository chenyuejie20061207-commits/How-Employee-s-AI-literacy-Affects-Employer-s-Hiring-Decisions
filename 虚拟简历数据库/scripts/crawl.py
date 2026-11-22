from __future__ import annotations

import argparse
import ipaddress
import json
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

import httpx

from common import (
    CONFIG_DIR,
    WORK_DIR,
    is_forbidden_source_url,
    load_yaml,
    normalize_source_url,
    normalize_url,
    utc_now,
)
from parse import parse_jd_html


class CrawlStopped(RuntimeError):
    pass


class DailyLimitReached(CrawlStopped):
    pass


class StateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.data = {"completed": {}, "daily_counts": {}}
        self.data.setdefault("completed", {})
        self.data.setdefault("daily_counts", {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def completed_at(self, url: str) -> str:
        value = self.data["completed"].get(url, "")
        return value.get("collected_at", "") if isinstance(value, dict) else str(value)

    def mark_completed(self, url: str, collected_at: str) -> None:
        self.data["completed"][url] = {"collected_at": collected_at}
        self.save()

    def request_count(self, day: str) -> int:
        return int(self.data["daily_counts"].get(day, 0))

    def increment_requests(self, day: str) -> None:
        self.data["daily_counts"][day] = self.request_count(day) + 1
        self.save()


class PoliteCrawler:
    def __init__(
        self,
        config: dict[str, Any],
        state: StateStore,
        client: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.config = config
        self.state = state
        self.client = client or httpx.Client(
            timeout=float(config.get("timeout_seconds", 15)),
            follow_redirects=False,
            headers={"User-Agent": config.get("user_agent", "ResearchMaterialBot/1.0")},
        )
        self._owns_client = client is None
        self.sleep = sleep
        self.monotonic = monotonic
        self.interval = max(6.0, float(config.get("request_interval_seconds", 6)))
        self.daily_limit = int(config.get("daily_limit", 100))
        self.max_retries = min(3, max(0, int(config.get("max_retries", 3))))
        self.user_agent = config.get("user_agent", "ResearchMaterialBot/1.0")
        self.last_request: dict[str, float] = {}

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> "PoliteCrawler":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def _validate_public_url(url: str) -> None:
        host = urlsplit(url).hostname or ""
        if host.lower() == "localhost" or host.lower().endswith(".local"):
            raise CrawlStopped("只允许公开网络地址")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return
        if not address.is_global:
            raise CrawlStopped("禁止访问私有、回环、链路本地或保留地址")

    def _wait(self, host: str, retry_after: float = 0) -> None:
        elapsed = self.monotonic() - self.last_request.get(host, -self.interval)
        delay = max(0.0, self.interval - elapsed, retry_after)
        if delay:
            self.sleep(delay)

    @staticmethod
    def _retry_after(value: str | None) -> float:
        if not value:
            return 0.0
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                target = parsedate_to_datetime(value)
                if target.tzinfo is None:
                    target = target.replace(tzinfo=timezone.utc)
                return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError):
                return 0.0

    @staticmethod
    def _has_access_challenge(text: str) -> bool:
        folded = text.casefold()
        return any(
            marker in folded
            for marker in ("captcha", "验证码", "安全验证", "访问验证", "滑动验证", "反爬验证")
        )

    def fetch(self, url: str, preserve_params: Iterable[str] | None = None) -> str:
        normalized = normalize_url(url, preserve_params)
        if is_forbidden_source_url(normalized):
            raise CrawlStopped("来源协议或robots明确排除，拒绝访问")
        self._validate_public_url(normalized)
        host = urlsplit(normalized).hostname or ""
        for attempt in range(self.max_retries + 1):
            day = datetime.now(timezone.utc).date().isoformat()
            if self.state.request_count(day) >= self.daily_limit:
                raise DailyLimitReached(f"已达到每日请求上限 {self.daily_limit}")
            self._wait(host)
            self.state.increment_requests(day)
            response = self.client.get(normalized, headers={"User-Agent": self.user_agent})
            self.last_request[host] = self.monotonic()

            if response.status_code in {401, 403}:
                raise CrawlStopped(f"HTTP {response.status_code}，立即停止且不重试")
            if response.status_code in {404, 410}:
                return ""
            if 300 <= response.status_code < 400:
                raise CrawlStopped("静态采集不跟随重定向")
            if response.status_code == 429 or response.status_code >= 500:
                if attempt >= self.max_retries:
                    raise CrawlStopped(f"HTTP {response.status_code}，重试次数已用完")
                self._wait(host, self._retry_after(response.headers.get("Retry-After")))
                continue
            if response.status_code != 200:
                raise CrawlStopped(f"不支持的 HTTP 状态 {response.status_code}")
            if self._has_access_challenge(response.text):
                raise CrawlStopped("检测到验证码或反爬验证，立即停止且不重试")
            return response.text
        raise AssertionError("不可达")


def crawl(config: dict[str, Any], preflight: dict[str, Any], state: StateStore) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    page_limit = max(1, int(config.get("page_limit", 10)))
    with PoliteCrawler(config, state) as crawler:
        for source in config.get("sources", []):
            if not source.get("enabled"):
                continue
            key = source.get("key", "")
            approval = preflight.get(key, {})
            if not approval.get("allowed") or not approval.get("terms_confirmed"):
                raise CrawlStopped(f"来源 {key} 未通过预检，拒绝采集")
            preserve_params = source.get("preserve_query_params", [])
            for raw_url in source.get("start_urls", [])[:page_limit]:
                url = normalize_source_url(raw_url, source)
                if state.completed_at(url):
                    continue
                html = crawler.fetch(url, preserve_params)
                if not html:
                    continue
                collected_at = utc_now()
                records.append(
                    parse_jd_html(
                        html,
                        source,
                        url,
                        collected_at=collected_at,
                        batch=str(source.get("batch", "crawl")),
                    )
                )
                state.mark_completed(url, collected_at)
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description="单线程、固定间隔、可断点续采的公开静态 JD 采集器")
    parser.add_argument("--config", default=str(CONFIG_DIR / "sources.yaml"))
    parser.add_argument("--preflight", default=str(WORK_DIR / "preflight_state.json"))
    parser.add_argument("--state", default=str(WORK_DIR / "crawl_state.json"))
    parser.add_argument("--output", required=True)
    parser.add_argument("--page-limit", type=int, default=None)
    args = parser.parse_args()

    config = load_yaml(args.config)
    if args.page_limit is not None:
        if args.page_limit < 1 or args.page_limit > 100:
            parser.error("--page-limit 必须在 1 到 100 之间")
        config["page_limit"] = args.page_limit
    preflight_path = Path(args.preflight)
    if not preflight_path.exists():
        parser.error("缺少预检状态；请先运行 preflight.py")
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    records = crawl(config, preflight, StateStore(args.state))

    output_path = Path(args.output)
    existing = []
    if output_path.exists():
        from export import read_csv

        existing = read_csv(output_path)
        records = existing + records
    from export import export_csv

    export_csv(records, output_path)
    print(f"本次新增 {len(records) - len(existing)} 条记录，输出总计 {len(records)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
