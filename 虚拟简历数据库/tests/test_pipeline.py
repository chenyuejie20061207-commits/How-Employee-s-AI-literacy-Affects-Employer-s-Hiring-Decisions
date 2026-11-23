from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from clean import clean_text, deduplicate_records
from common import (
    ENTRY_DEIDENTIFICATION_STATUS,
    ENTRY_FIELD_TYPES,
    JOB_TYPES,
    SCHEMA_FIELDS,
    STRUCTURE_DEIDENTIFICATION_STATUS,
    city_tier,
    load_taxonomy,
    normalize_source_url,
    normalize_url,
    sha256_text,
)
from crawl import CrawlStopped, PoliteCrawler, StateStore
from deidentify import mask_pii, scan_pii
from export import export_csv, read_csv
from import_resume_entries import import_entry_record
from manual_import import UnsafeResumeInput, import_structure_record, validate_abstract_structure
from parse import classify_job, parse_jd_html
from preflight import preflight_source
from validate import validate_records


def jd_text(marker: str = "") -> str:
    return ("负责业务需求沟通、流程建设与跨部门协作，形成清晰文档并持续改进工作质量。" + marker) * 4


def make_jd(index: int = 0, job_type: str = "常规型", city: str = "北京") -> dict[str, str]:
    text = jd_text(str(index))
    return {
        "素材ID": f"JD-{index:04d}",
        "来源类型": "公开招聘平台",
        "岗位类型": job_type,
        "行业": "互联网",
        "城市": city,
        "字段类型": "岗位JD",
        "文本内容": text,
        "采集时间": "2026-01-01T08:00:00Z",
        "来源记录ID": f"record-{index}",
        "来源平台": "合成静态页",
        "来源URL": f"https://jobs{index % 4}.example/jobs/{index}",
        "其他来源URL": "",
        "原始岗位ID": f"record-{index}",
        "采集批次": "synthetic",
        "原始岗位名称": "行政文员",
        "企业名称": f"合成企业{index % 40}",
        "企业性质": "民营企业",
        "企业规模": "100-499人",
        "分类理由": "合成测试分类",
        "AI素养信号等级": "不适用",
        "文本哈希": sha256_text(clean_text(text)),
        "重复组ID": "",
        "脱敏状态": "已脱敏",
        "人工复核状态": "已通过",
    }


def make_structure(index: int, job_type: str, field_type: str) -> dict[str, str]:
    text = f"抽象结构：字段顺序为学历层次、专业类别、课程模块；合成编号{index}。"
    return {
        **make_jd(index + 1000, job_type, "上海"),
        "素材ID": f"STRUCT-{index:04d}",
        "来源类型": "公开简历模板",
        "字段类型": field_type,
        "文本内容": text,
        "原始岗位名称": "",
        "企业名称": "",
        "企业性质": "",
        "企业规模": "",
        "文本哈希": sha256_text(clean_text(text)),
        "脱敏状态": STRUCTURE_DEIDENTIFICATION_STATUS,
    }


JOB_ABBR = {"IT/技术类": "IT", "管理类": "MGT", "非常规认知型": "NRC", "常规型": "REG"}
MOD_ABBR = {"教育经历条目": "EDU", "实习经历条目": "INT", "项目经历条目": "PRJ", "技能条目": "SKL"}


def make_entry(index: int, job_type: str, field_type: str) -> dict[str, str]:
    if field_type == "教育经历条目":
        text = f"2020.09–2024.06 某211高校 专业{index}(本科)；主修方向：合成测试方向{index}。"
    elif field_type == "实习经历条目":
        text = f"合成机构{index}|合成实习生|2022.06–2022.09\n- 负责合成事项{index}；\n- 协助完成任务约{index}项。"
    elif field_type == "项目经历条目":
        text = f"合成项目{index}|成员|2023.03–2023.06\n- 负责合成模块{index}；\n- 参与交付文档约{index}份。"
    else:
        text = f"合成技能簇{index}：合成技能（熟练，含证据占位）；第二技能（掌握）；第三技能（了解）。"
    entry_id = f"VRE-{JOB_ABBR[job_type]}-{MOD_ABBR[field_type]}-{index:02d}"
    return {
        **make_jd(index + 2000, job_type, "不适用"),
        "素材ID": entry_id,
        "来源类型": "公开简历模板",
        "字段类型": field_type,
        "文本内容": text,
        "原始岗位名称": "",
        "企业名称": f"合成机构{index % 40}",
        "文本哈希": sha256_text(clean_text(text)),
        "脱敏状态": ENTRY_DEIDENTIFICATION_STATUS,
    }


AI_LEVELS = ("L0", "L1", "L2", "L3", "L4", "L5")


def make_ai_entry(level: str, job_type: str, field_type: str) -> dict[str, str]:
    li = int(level[1:])
    anchor = {
        0: "手工绘制流程图并纸质归档", 1: "用Excel函数汇总数据",
        2: "编写Python脚本自动清洗数据表", 3: "调用机器学习库训练分类模型",
        4: "搭建知识库问答Agent检索答疑", 5: "设计模型评估框架与落地规划",
    }[li]
    nn = f"{li + 1:02d}"
    tag = f"{JOB_ABBR[job_type]}{MOD_ABBR[field_type]}{nn}"
    if field_type == "教育经历条目":
        text = f"2020.09–2024.06 某211高校 专业甲(本科)；主修方向：{anchor}；课程实训覆盖需求梳理、实现与验收全流程并完成课程设计约3门,获实训优秀1次。{tag}。"
    elif field_type == "实习经历条目":
        text = f"合成机构{tag}|合成实习生|2022.06–2022.09\n- {anchor}，覆盖事项约5项；\n- 协助完成任务约20项；\n- 参与评审并整理文档约3份。"
    elif field_type == "项目经历条目":
        text = f"合成项目{tag}|成员|2023.03–2023.06\n- {anchor}，覆盖模块约5个；\n- 协助完成交付约10项；\n- 参与联调并整理文档约3份。"
    else:
        text = f"专业技能：{anchor}（掌握，含证据约5项）；文档写作（熟练，整理说明约5份）；团队协作（掌握，参与评审约10次）；资料整理（了解，归档约20份）。{tag}。"
    entry_id = f"VAI-{JOB_ABBR[job_type]}-{MOD_ABBR[field_type]}-{nn}"
    is_org = field_type in ("实习经历条目", "项目经历条目")
    return {
        **make_jd(3000 + li, job_type, "不适用"),
        "素材ID": entry_id,
        "来源类型": "公开简历模板",
        "字段类型": field_type,
        "文本内容": text,
        "原始岗位名称": "",
        "企业名称": f"合成机构{tag}" if is_org else "不适用",
        "AI素养信号等级": level,
        "文本哈希": sha256_text(clean_text(text)),
        "脱敏状态": ENTRY_DEIDENTIFICATION_STATUS,
    }


class FakeResponse:
    def __init__(self, status_code: int, text: str = "", headers: dict[str, str] | None = None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


class FakeClient:
    def __init__(self, responses: list[FakeResponse]):
        self.responses = responses
        self.calls = []

    def get(self, url: str, headers: dict[str, str]):
        self.calls.append((url, headers))
        return self.responses.pop(0)


def source_config() -> dict:
    return {
        "key": "synthetic",
        "name": "合成来源",
        "source_type": "公开招聘平台",
        "base_url": "https://public.example/",
        "start_urls": ["https://public.example/jobs/1?utm_source=test&ref=required-attribution"],
        "public_static": True,
        "preserve_query_params": ["ref"],
        "terms": {"manual_confirmed": True, "reference": "人工合成确认"},
    }


def test_clean_text_preserves_chinese_and_normalizes_whitespace():
    assert clean_text("  教育\u3000背景\r\n\r\n\r\n  项目Ａ\x00  ") == "教育 背景\n\n项目A"


def test_csv_bom_embedded_newline_comma_and_quotes(tmp_path: Path):
    path = tmp_path / "合成.csv"
    record = make_jd()
    record["文本内容"] = "第一行,含逗号\n第二行含\"引号\"与中文"
    export_csv([record], path)
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        raw_rows = list(csv.reader(handle))
    assert raw_rows[0] == list(SCHEMA_FIELDS)
    assert len(raw_rows) == 2
    loaded = read_csv(path)
    assert loaded[0]["文本内容"] == record["文本内容"]
    assert tuple(loaded[0]) == SCHEMA_FIELDS


def test_pii_scan_and_mask_do_not_leak_values():
    text = "联系人：张三，手机13812345678，邮箱test@example.com，微信：abcdef1"
    findings = scan_pii(text)
    masked, returned = mask_pii(text)
    assert {item["type"] for item in findings} >= {"联系人姓名", "手机号", "邮箱", "微信号"}
    assert returned == findings
    assert "13812345678" not in masked
    assert "test@example.com" not in masked
    assert "张三" not in masked
    assert all("match" not in item for item in findings)


def test_exact_dedup_and_near_duplicate_candidates():
    records = [make_jd(1), make_jd(2), make_jd(3)]
    records[1]["文本内容"] = records[0]["文本内容"]
    records[1]["文本哈希"] = records[0]["文本哈希"]
    records[2]["文本内容"] = records[0]["文本内容"][:-1] + "异"
    unique, report = deduplicate_records(records, threshold=92)
    assert len(unique) == 2
    assert len(report["exact_removed"]) == 1
    assert len(report["near_duplicate_candidates"]) == 1
    assert report["near_duplicate_candidates"][0]["score"] >= 92
    assert all(record["重复组ID"].startswith("NEAR-") for record in unique)


def test_classification_priority_and_unmatched():
    taxonomy = load_taxonomy()
    job_type, reason = classify_job("软件项目经理", "负责团队管理和 Python 开发", taxonomy)
    assert job_type == "IT/技术类"
    assert "优先级" in reason
    assert classify_job("完全未知岗位", "无关键词", taxonomy)[0] == ""


def test_url_normalization_and_city_tiers():
    assert normalize_url("HTTPS://Example.COM:443/jobs/?b=2&utm_source=x&a=1&ref=tracking#top") == "https://example.com/jobs?a=1&b=2"
    source = source_config()
    assert normalize_source_url(source["start_urls"][0], source) == "https://public.example/jobs/1?ref=required-attribution"
    taxonomy = load_taxonomy()
    assert city_tier("北京", taxonomy) == "一线城市"
    assert city_tier("德阳", taxonomy) == "其他城市"
    assert city_tier("", taxonomy) == "未分层"


def test_synthetic_html_parse_and_deidentify():
    html = """
    <html><head><meta name='job-id' content='JOB-中文-1'></head><body>
    <h1 class='title'>Python 开发工程师</h1>
    <div class='description'>负责平台开发、需求分析、测试与文档建设，联系电话13812345678。
    需要撰写清晰的技术方案并协作完成交付，持续优化质量与研发流程，参与代码评审和公共模块建设。</div>
    <span class='industry'>互联网</span><span class='city'>杭州</span>
    <span class='company'>合成企业</span><span class='nature'>民营企业</span><span class='size'>100-499人</span>
    </body></html>
    """
    source = source_config()
    source["selectors"] = {
        "title": "h1.title",
        "jd": "div.description",
        "industry": ".industry",
        "city": ".city",
        "company": ".company",
        "company_nature": ".nature",
        "company_size": ".size",
        "source_record_id": "meta[name=job-id]",
    }
    source["source_record_id_attribute"] = "content"
    record = parse_jd_html(html, source, source["start_urls"][0], collected_at="2026-01-01T08:00:00Z")
    assert record["岗位类型"] == "IT/技术类"
    assert record["城市"] == "杭州"
    assert record["来源记录ID"] == "JOB-中文-1"
    assert "13812345678" not in record["文本内容"]
    assert record["来源URL"] == "https://public.example/jobs/1?ref=required-attribution"
    assert record["脱敏状态"] == "已脱敏"


def structure_input(text: str = "抽象结构：字段顺序为学历层次、专业类别、课程模块。") -> dict[str, str]:
    return {
        "来源类型": "公开简历模板",
        "岗位类型": "常规型",
        "行业": "互联网",
        "城市": "北京",
        "字段类型": "教育结构",
        "文本内容": text,
        "来源记录ID": "abstract-1",
        "来源平台": "合成模板来源",
        "来源URL": "https://templates.example/item/1",
        "分类理由": "人工抽象为字段结构",
    }


def test_manual_import_accepts_only_abstract_traceable_structure():
    record = import_structure_record(structure_input(), collected_at="2026-01-01T08:00:00Z")
    assert record["素材ID"].startswith("STRUCT-")
    assert record["人工复核状态"] == "待复核"
    assert record["脱敏状态"] == STRUCTURE_DEIDENTIFICATION_STATUS
    assert record["文本内容"].startswith("抽象结构:")
    with pytest.raises(UnsafeResumeInput):
        import_structure_record(structure_input("个人简历 姓名：张三 手机13812345678"))
    with pytest.raises(UnsafeResumeInput):
        import_structure_record(structure_input("抽象结构：学历层次、专业类别，联系电话13812345678"))
    bad_source = structure_input()
    bad_source["来源URL"] = "file:///tmp/private.html"
    with pytest.raises(ValueError):
        import_structure_record(bad_source)


def test_preflight_missing_robots_and_unconfirmed_terms_are_not_permission():
    missing = preflight_source(source_config(), FakeClient([FakeResponse(404)]))
    assert missing["robots_status"] == "missing"
    assert not missing["allowed"]
    unconfirmed_source = source_config()
    unconfirmed_source["terms"]["manual_confirmed"] = False
    unconfirmed = preflight_source(unconfirmed_source, FakeClient([]))
    assert not unconfirmed["allowed"]
    allowed = preflight_source(
        source_config(), FakeClient([FakeResponse(200, "User-agent: *\nDisallow:")])
    )
    assert allowed["allowed"]
    assert list(allowed["can_fetch"]) == ["https://public.example/jobs/1?ref=required-attribution"]


def test_crawler_stops_on_403_without_retry(tmp_path: Path):
    client = FakeClient([FakeResponse(403), FakeResponse(200, "不应访问")])
    config = {"request_interval_seconds": 6, "daily_limit": 10, "max_retries": 3}
    crawler = PoliteCrawler(config, StateStore(tmp_path / "state.json"), client=client, sleep=lambda _: None)
    with pytest.raises(CrawlStopped):
        crawler.fetch("https://public.example/jobs/1")
    assert len(client.calls) == 1


def test_forbidden_sources_are_rejected_before_network_and_in_final_data(tmp_path: Path):
    client = FakeClient([FakeResponse(200, "不应访问")])
    config = {"request_interval_seconds": 6, "daily_limit": 10, "max_retries": 3}
    crawler = PoliteCrawler(config, StateStore(tmp_path / "forbidden.json"), client=client, sleep=lambda _: None)
    with pytest.raises(CrawlStopped):
        crawler.fetch("https://job.ncss.cn/student/jobs/example/detail.html")
    assert len(client.calls) == 0

    record = make_jd()
    record["其他来源URL"] = "https://campus.ncss.cn/jobs/example"
    report = validate_records([record])
    assert report["error_counts"]["forbidden_source"] == 1


def test_crawler_honors_retry_after_and_captcha_stop(tmp_path: Path):
    client = FakeClient(
        [FakeResponse(429, headers={"Retry-After": "12"}), FakeResponse(200, "公开静态内容")]
    )
    sleeps = []
    config = {"request_interval_seconds": 6, "daily_limit": 10, "max_retries": 3}
    crawler = PoliteCrawler(
        config, StateStore(tmp_path / "retry.json"), client=client, sleep=sleeps.append
    )
    assert crawler.fetch("https://public.example/jobs/1") == "公开静态内容"
    assert 12 in sleeps
    assert len(client.calls) == 2

    captcha_client = FakeClient([FakeResponse(200, "请完成验证码"), FakeResponse(200, "不应访问")])
    captcha = PoliteCrawler(
        config, StateStore(tmp_path / "captcha.json"), client=captcha_client, sleep=lambda _: None
    )
    with pytest.raises(CrawlStopped):
        captcha.fetch("https://public.example/jobs/2")
    assert len(captcha_client.calls) == 1


def test_crawler_preserves_required_ref_attribution(tmp_path: Path):
    client = FakeClient([FakeResponse(200, "公开静态内容")])
    config = {"request_interval_seconds": 6, "daily_limit": 10, "max_retries": 3}
    crawler = PoliteCrawler(
        config, StateStore(tmp_path / "attribution.json"), client=client, sleep=lambda _: None
    )
    crawler.fetch(
        "https://public.example/jobs/1?utm_source=remove&ref=required-attribution",
        preserve_params=["ref"],
    )
    assert client.calls[0][0] == "https://public.example/jobs/1?ref=required-attribution"


def test_schema_enum_hash_url_time_length_and_pii_validation():
    record = make_jd()
    record["岗位类型"] = "不存在的类型"
    record["来源URL"] = "javascript:alert(1)"
    record["采集时间"] = "2026-01-01"
    record["文本内容"] = "太短13812345678"
    report = validate_records([record])
    codes = report["error_counts"]
    for code in ("invalid_enum", "invalid_url", "invalid_timestamp", "invalid_text_length", "pii_residual", "hash_mismatch"):
        assert code in codes
    assert not report["passed"]
    jd_with_structure_status = make_jd(100)
    jd_with_structure_status["脱敏状态"] = STRUCTURE_DEIDENTIFICATION_STATUS
    status_report = validate_records([jd_with_structure_status], strict=True)
    assert status_report["error_counts"]["invalid_enum"] == 1
    assert status_report["error_counts"]["not_deidentified"] == 1


def test_automatic_review_status_is_valid_but_not_human_signoff():
    record = make_jd()
    record["人工复核状态"] = "自动校验通过（待人工复核）"
    normal = validate_records([record], strict=False)
    assert normal["passed"]
    strict = validate_records([record], strict=True)
    assert strict["error_counts"]["review_not_passed"] == 1
    assert not strict["passed"]


def test_stratified_counts_matrix_and_strict_shortage():
    record = make_jd()
    report = validate_records([record], strict=False)
    assert report["passed"]
    assert not report["target_met"]
    assert len(report["jd_strata"]) == 16
    assert len(report["structure_matrix"]) == 16
    assert len(report["entry_matrix"]) == 16
    assert report["jd_strata"]["常规型|北京"]["count"] == 1
    assert "常规型|一线城市" not in report["jd_strata"]
    other_city = validate_records([make_jd(99, city="杭州")])
    assert other_city["error_counts"]["unknown_stratum"] == 1
    strict = validate_records([record], strict=True)
    assert not strict["passed"]
    assert strict["error_counts"]["target_gap"] == 148
    assert not strict["target_met"]


def test_full_synthetic_targets_pass_strict_mode():
    taxonomy = load_taxonomy()
    job_types = taxonomy["classification_priority"]
    cities = taxonomy["jd_cities"]
    records = []
    index = 0
    for job_type in job_types:
        for city in cities:
            for _ in range(25):
                records.append(make_jd(index, job_type, city))
                index += 1
    field_types = ("教育结构", "实习结构", "项目结构", "技能结构")
    structure_index = 0
    for job_type in job_types:
        for field_type in field_types:
            for _ in range(5):
                records.append(make_structure(structure_index, job_type, field_type))
                structure_index += 1
    entry_index = 0
    for job_type in job_types:
        for field_type in ENTRY_FIELD_TYPES:
            for _ in range(5):
                records.append(make_entry(entry_index, job_type, field_type))
                entry_index += 1
    for job_type in job_types:
        for field_type in ENTRY_FIELD_TYPES:
            for level in AI_LEVELS:
                records.append(make_ai_entry(level, job_type, field_type))
    report = validate_records(records, strict=True)
    assert report["counts"] == {
        "total": 656, "jd": 400, "structures": 80, "entries": 80, "ai_entries": 96,
    }
    assert report["target_met"]
    assert report["passed"], json.dumps(report["errors"][:5], ensure_ascii=False)
    json.dumps(report, ensure_ascii=False)


def entry_values(**overrides) -> dict[str, str]:
    base = {
        "素材ID": "VRE-IT-INT-01",
        "岗位类型": "IT/技术类",
        "字段类型": "实习经历条目",
        "文本内容": "合成机构甲|合成实习生|2022.06–2022.09\n- 负责合成接口开发与联调；\n- 协助整理接口文档约3份。",
        "来源类型": "公开简历模板",
        "行业": "信息技术/互联网",
        "城市": "不适用",
        "来源记录ID": "guide-qmjianli-3489-IT-INT-vent-01",
        "来源平台": "全民简历",
        "来源URL": "https://www.qmjianli.com/content/guide/3489",
        "其他来源URL": "",
        "采集批次": "public-guide-entries-v1",
        "采集时间": "2026-11-23T02:20:00+08:00",
        "企业名称": "岚潮信息科技有限公司",
        "企业性质": "民营企业",
        "企业规模": "100-499人",
        "分类理由": "合成测试分类理由",
        "AI素养信号等级": "不适用",
    }
    base.update(overrides)
    return base


def test_entry_import_accepts_fabricated_entry():
    record = import_entry_record(entry_values())
    assert record["脱敏状态"] == ENTRY_DEIDENTIFICATION_STATUS
    assert record["人工复核状态"] == "自动校验通过（待人工复核）"
    assert record["重复组ID"] == ""
    assert record["文本哈希"] == sha256_text(clean_text(record["文本内容"]))
    assert record["来源类型"] == "公开简历模板"


def test_entry_import_rejects_raw_resume_markers_and_pii():
    with pytest.raises(UnsafeResumeInput):
        import_entry_record(entry_values(文本内容="个人简历模板改写|实习生|2022.06–2022.09\n- 负责事项甲；\n- 协助事项乙。"))
    with pytest.raises(UnsafeResumeInput):
        import_entry_record(entry_values(文本内容="合成机构乙|实习生|2022.06–2022.09\n- 负责事项甲，联系电话13812345678；\n- 协助事项乙。"))


def test_entry_import_rejects_bad_provenance_and_ids():
    with pytest.raises(UnsafeResumeInput):
        import_entry_record(entry_values(来源URL="https://job.ncss.cn/guide/1"))
    with pytest.raises(UnsafeResumeInput):
        import_entry_record(entry_values(来源记录ID=""))
    with pytest.raises(UnsafeResumeInput):
        import_entry_record(entry_values(素材ID="RES-IT-INT-01"))
    with pytest.raises(UnsafeResumeInput):
        import_entry_record(entry_values(素材ID="VRE-MGT-INT-01"))
    with pytest.raises(UnsafeResumeInput):
        import_entry_record(entry_values(**{"企业名称": "不适用"}))
    with pytest.raises(UnsafeResumeInput):
        import_entry_record(
            entry_values(
                **{
                    "字段类型": "教育经历条目",
                    "素材ID": "VRE-IT-EDU-01",
                    "文本内容": "2020.09–2024.06 某211高校 专业甲(本科)；主修方向：合成。",
                    "企业名称": "岚潮信息科技有限公司",
                    "行业": "未披露",
                    "企业性质": "未披露",
                    "企业规模": "未披露",
                }
            )
        )


def test_entry_deidentification_status_is_family_exclusive():
    entry_wrong_status = make_entry(1, "IT/技术类", "技能条目")
    entry_wrong_status["脱敏状态"] = STRUCTURE_DEIDENTIFICATION_STATUS
    report = validate_records([entry_wrong_status])
    assert report["error_counts"]["invalid_enum"] == 1
    structure_wrong_status = make_structure(1, "IT/技术类", "技能结构")
    structure_wrong_status["脱敏状态"] = ENTRY_DEIDENTIFICATION_STATUS
    report = validate_records([structure_wrong_status])
    assert report["error_counts"]["invalid_enum"] == 1
    jd_wrong_status = make_jd(2)
    jd_wrong_status["脱敏状态"] = ENTRY_DEIDENTIFICATION_STATUS
    report = validate_records([jd_wrong_status])
    assert report["error_counts"]["invalid_enum"] == 1


def test_entry_length_branch_and_near_duplicate_candidates():
    short_entry = make_entry(2, "IT/技术类", "技能条目")
    short_entry["文本内容"] = "太短"
    short_entry["文本哈希"] = sha256_text(clean_text("太短"))
    report = validate_records([short_entry])
    assert report["error_counts"]["invalid_text_length"] == 1
    long_entry = make_entry(3, "IT/技术类", "技能条目")
    long_entry["文本内容"] = "合成" * 300
    long_entry["文本哈希"] = sha256_text(clean_text(long_entry["文本内容"]))
    report = validate_records([long_entry])
    assert report["error_counts"]["invalid_text_length"] == 1

    left = make_entry(10, "IT/技术类", "实习经历条目")
    right = make_entry(11, "IT/技术类", "实习经历条目")
    right["文本内容"] = left["文本内容"].replace("合成事项10", "合成事项11").replace("约10项", "约11项")
    right["文本哈希"] = sha256_text(clean_text(right["文本内容"]))
    _, report = deduplicate_records([left, right], threshold=50)
    pairs = {(item["left_id"], item["right_id"]) for item in report["near_duplicate_candidates"]}
    assert (left["素材ID"], right["素材ID"]) in pairs


def ai_entry_values(level: str = "L2", **overrides) -> dict[str, str]:
    li = int(level[1:])
    text = (
        f"合成机构乙|合成实习生|2022.06–2022.09\n"
        f"- 编写脚本完成数据处理约{li + 3}类；\n- 协助整理文档约5份；\n- 参与评审约3次。"
        f"补充说明：课程实训覆盖需求梳理、实现与验收全流程。"
    )
    base = entry_values(
        **{
            "素材ID": f"VAI-IT-INT-0{li + 1}",
            "文本内容": text,
            "AI素养信号等级": level,
            "来源记录ID": f"guide-qmjianli-3489-IT-INT-vai-0{li + 1}",
            "采集批次": "public-guide-ai-entries-v1",
        }
    )
    base.update(overrides)
    return base


def test_ai_entry_import_accepts_levels_and_id_consistency():
    for level in ("L0", "L1", "L2", "L3", "L4", "L5"):
        record = import_entry_record(ai_entry_values(level))
        assert record["AI素养信号等级"] == level
        assert record["素材ID"].startswith("VAI-")
    with pytest.raises(UnsafeResumeInput):
        import_entry_record(ai_entry_values("L2", **{"素材ID": "VAI-IT-INT-05"}))
    with pytest.raises(UnsafeResumeInput):
        import_entry_record(ai_entry_values(**{"AI素养信号等级": "不适用"}))


def test_ai_entry_family_level_exclusive():
    vre_with_level = entry_values(**{"AI素养信号等级": "L2"})
    with pytest.raises(UnsafeResumeInput):
        import_entry_record(vre_with_level)
    jd_with_level = make_jd(7)
    jd_with_level["AI素养信号等级"] = "L3"
    report = validate_records([jd_with_level])
    assert report["error_counts"]["invalid_enum"] >= 1
    ai_missing_level = make_ai_entry("L0", "IT/技术类", "技能条目")
    ai_missing_level["AI素养信号等级"] = "不适用"
    report = validate_records([ai_missing_level])
    assert report["error_counts"]["invalid_enum"] >= 1


def test_ai_entry_matrix_quota_and_gap():
    full = [
        make_ai_entry(level, job_type, field_type)
        for job_type in JOB_TYPES
        for field_type in ENTRY_FIELD_TYPES
        for level in AI_LEVELS
    ]
    report = validate_records(full, strict=False)
    assert report["counts"]["ai_entries"] == 96
    assert report["target_met"] is False
    ai_gaps = [g for g in report["gaps"] if g["type"] == "ai_entry_cell"]
    assert ai_gaps == []
    missing = full[:-1]
    report = validate_records(missing, strict=False)
    ai_gaps = [g for g in report["gaps"] if g["type"] == "ai_entry_cell"]
    assert len(ai_gaps) == 1


def test_ai_entry_length_bounds():
    short = make_ai_entry("L0", "IT/技术类", "技能条目")
    short["文本内容"] = "太短"
    short["文本哈希"] = sha256_text(clean_text("太短"))
    report = validate_records([short])
    assert report["error_counts"]["invalid_text_length"] == 1
    long = make_ai_entry("L5", "IT/技术类", "技能条目")
    long["文本内容"] = "合成" * 200
    long["文本哈希"] = sha256_text(clean_text(long["文本内容"]))
    report = validate_records([long])
    assert report["error_counts"]["invalid_text_length"] == 1


def test_ai_entry_sibling_hashes_distinct():
    siblings = [make_ai_entry(level, "IT/技术类", "实习经历条目") for level in AI_LEVELS]
    hashes = {row["文本哈希"] for row in siblings}
    assert len(hashes) == 6
