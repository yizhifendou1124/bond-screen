import hashlib
import html
import json
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit
from email.utils import parsedate_to_datetime

import requests


SHANGHAI = ZoneInfo("Asia/Shanghai")


def now():
    return datetime.now(SHANGHAI)


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def plain(text):
    text = re.sub(r"<(?:script|style)\b.*?</(?:script|style)>", " ", text, flags=re.S | re.I)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text))).strip()


def percent(text):
    text = str(text).strip().removesuffix("%")
    if text in ("", "--", "-", "None"):
        return None
    number = Decimal(text)
    if not number.is_finite() or number < -100:
        raise ValueError("无效收益率")
    return str(number)


class Client:
    def __init__(self, evidence, timeout=25, retries=5, interval=0.12):
        self.evidence = Path(evidence)
        self.timeout = timeout
        self.retries = retries
        self.local = threading.local()
        self.lock = threading.Lock()
        self.manifest = []
        self.interval = interval
        self.host_intervals = {}
        self.next_allowed = {}

    def pace(self, url):
        host = urlsplit(url).netloc
        while True:
            with self.lock:
                current = time.monotonic()
                wait = self.next_allowed.get(host, 0) - current
                if wait <= 0:
                    self.next_allowed[host] = current + self.host_intervals.get(host, self.interval)
                    return
            time.sleep(min(wait, 1))

    def throttle(self, url, response, attempt):
        host = urlsplit(url).netloc
        pause = min(120, 30 * 2 ** attempt)
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                requested = int(retry_after) if retry_after.isdigit() else (parsedate_to_datetime(retry_after) - now()).total_seconds()
                pause = max(pause, requested)
            except (TypeError, ValueError, OverflowError):
                pass
        with self.lock:
            self.host_intervals[host] = min(2, max(0.24, self.host_intervals.get(host, self.interval) * 2))
            self.next_allowed[host] = max(self.next_allowed.get(host, 0), time.monotonic() + pause)
            self.manifest.append({"url": url, "at": now().isoformat(), "status": "source_throttled", "http_status": response.status_code, "cooldown_seconds": pause})

    def get(self, url, params=None, binary=False):
        if not hasattr(self.local, "session"):
            self.local.session = requests.Session()
            self.local.session.headers.update({"User-Agent": "Mozilla/5.0", "Referer": "https://fundf10.eastmoney.com/"})
        for attempt in range(self.retries):
            try:
                self.pace(url)
                response = self.local.session.get(url, params=params, timeout=self.timeout, stream=True)
                chunks = []
                size = 0
                with response:
                    response.raise_for_status()
                    for chunk in response.iter_content(65536):
                        size += len(chunk)
                        if size > 40 * 1024 * 1024:
                            raise ValueError("响应超过40MiB，停止自动解析")
                        chunks.append(chunk)
                content = b"".join(chunks)
                digest = hashlib.sha256(content).hexdigest()
                identifier = hashlib.sha256(response.url.encode()).hexdigest()
                path = self.evidence / (identifier + "_" + digest + (".pdf" if binary else ".txt"))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                with self.lock:
                    self.manifest.append({"url": response.url, "fetched_at": now().isoformat(), "sha256": digest, "path": str(path.relative_to(self.evidence.parent))})
                return content if binary else content.decode("utf-8-sig")
            except (requests.RequestException, UnicodeError) as error:
                response = getattr(error, "response", None)
                if response is not None and response.status_code in (429, 503, 514):
                    self.throttle(url, response, attempt)
                if attempt + 1 == self.retries:
                    raise RuntimeError(f"数据请求失败: {url}: {error}") from error
                time.sleep(min(8, 2 ** attempt))
        raise RuntimeError("请求未完成")

    def json(self, url, params=None):
        return json.loads(self.get(url, params))


def parallel(items, worker, workers, label):
    results = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(worker, item): item for item in items}
        for position, future in enumerate(as_completed(futures), 1):
            item = futures[future]
            try:
                results[item] = {"status": "ok", "data": future.result()}
            except Exception as error:
                results[item] = {"status": "failed", "error": f"{type(error).__name__}: {error}"}
            if position % 100 == 0 or position == len(futures):
                counts = Counter(entry["status"] for entry in results.values())
                print(f"{label}: {position}/{len(futures)} {dict(counts)}", flush=True)
    return results


def parse_catalog(text):
    match = re.fullmatch(r"\s*var\s+r\s*=\s*(\[.*\])\s*;?\s*", text, re.S)
    if not match:
        raise ValueError("市场目录结构改变，拒绝继续")
    records = json.loads(match.group(1))
    if not records or any(not isinstance(row, list) or len(row) < 4 or not re.fullmatch(r"\d{6}", str(row[0])) for row in records):
        raise ValueError("市场目录记录无效")
    if len({row[0] for row in records}) != len(records):
        raise ValueError("市场目录含重复代码")
    return {row[0]: row for row in records if row[3].startswith("债券型") or (row[3].startswith("QDII") and "债" in row[3])}


def parse_rank(text):
    match = re.search(r"datas\s*:\s*(\[.*?\])\s*,\s*allRecords\s*:\s*(\d+)", text, re.S)
    if not match:
        raise ValueError("排名分页结构改变，拒绝继续")
    rows = [row.split(",") for row in json.loads(match.group(1))]
    if any(len(row) < 17 or not re.fullmatch(r"\d{6}", row[0]) for row in rows):
        raise ValueError("排名记录字段无效")
    return rows, int(match.group(2))


def ranking(client, kind, page_size=500):
    result = {}
    total = None
    page = 1
    while total is None or len(result) < total:
        text = client.get("https://fund.eastmoney.com/data/rankhandler.aspx", {"op": "ph", "dt": "kf", "ft": kind, "sc": "jnzf", "st": "desc", "pi": page, "pn": page_size, "dx": 0})
        rows, reported_total = parse_rank(text)
        if total is not None and total != reported_total:
            raise ValueError("分页期间排名总数变化，请重新执行")
        total = reported_total
        if not rows and len(result) != total:
            raise ValueError("排名分页提前结束")
        for row in rows:
            if row[0] in result:
                raise ValueError("排名分页重复，拒绝把不完整结果作为全市场")
            result[row[0]] = row
        if len(result) > total:
            raise ValueError("排名计数超过声明总数")
        page += 1
    return result


def parse_phase(text):
    text = plain(text)
    match = re.search(r"截止至\s+(\d{4}-\d{2}-\d{2}).*?近1周\s+近1月\s+近3月\s+近6月\s+今年来.*?阶段涨幅\s+([\d.%-]+)\s+([\d.%-]+)\s+([\d.%-]+)\s+([\d.%-]+)\s+([\d.%-]+)", text)
    if not match:
        raise ValueError("阶段收益表未取得")
    return {"data_date": match.group(1), "recent_3m": percent(match.group(4)), "recent_6m": percent(match.group(5)), "ytd": percent(match.group(6))}


def parse_metadata(text):
    text = plain(text)
    fields = {}
    for field, pattern in [("full_name", r"基金全称\s+(.*?)\s+基金简称"), ("manager", r"基金管理人\s+(.*?)\s+基金托管人"), ("short_name", r"基金简称\s+(.*?)\s+基金代码")]:
        match = re.search(pattern, text)
        fields[field] = match.group(1) if match else None
    inception = re.search(r"成立日期\s*/\s*规模\s+(\d{4})年(\d{2})月(\d{2})日", text)
    fields["inception_date"] = "-".join(inception.groups()) if inception else None
    if not fields["full_name"] or not fields["manager"] or not fields["inception_date"] or not fields["short_name"]:
        raise ValueError("身份、成立时间或份额名称未确认")
    fields["currency"] = "USD" if "美元" in fields["short_name"] else "HKD" if "港币" in fields["short_name"] else "CNY"
    date.fromisoformat(fields["inception_date"])
    return fields


def parse_annual(text, inception, end_year):
    match = re.search(r'content:"((?:\\.|[^"\\])*)"', text)
    if not match:
        raise ValueError("年度数据结构改变")
    content = match.group(1).replace('\\"', '"')
    rows = re.findall(r"<tr\b[^>]*>(.*?)</tr>", content, re.S)[:2]
    cells = [[plain(cell) for cell in re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", row, re.S)] for row in rows]
    if not cells and (not content.strip() or "暂无" in plain(content)):
        return {}
    if len(cells) != 2 or not cells[1] or cells[1][0] != "阶段涨幅" or len(cells[0]) != len(cells[1]):
        raise ValueError("年度收益行未确认")
    annual = {}
    for label, value in zip(cells[0][1:], cells[1][1:]):
        if not re.match(r"^\d{4}", label):
            continue
        year = int(label[:4])
        if 2020 <= year <= end_year:
            annual[str(year)] = percent(value) if inception <= f"{year}-01-01" else None
    return annual


def parse_subscription(text, checked_on):
    text = plain(text)
    match = re.search(r"交易状态[:：]\s*(.*?)(?:购买手续费|该基金暂不开放购买|免费开户)", text)
    context = match.group(1).strip() if match else ""
    quota = re.search(r"单日累计购买上限\s*([0-9]+(?:\.[0-9]+)?)\s*(亿|万)?元", context)
    limit = str(Decimal(quota.group(1)) * {"亿": Decimal(100000000), "万": Decimal(10000), None: Decimal(1)}[quota.group(2)]) if quota else None
    if "该基金暂不开放购买" in text or "暂停申购" in context or "封闭期" in context or (limit is not None and Decimal(limit) == 0):
        status = "closed"
    elif "限大额" in context:
        status = "limited"
    elif "开放申购" in context:
        status = "open"
    else:
        status = "unverified"
    return {"subscription_status": status, "subscription_checked_on": checked_on, "subscription_channel": "天天基金", "subscription_limit_cny": limit, "subscription_text": context}


def metadata(client, code):
    source = f"https://fundf10.eastmoney.com/jbgk_{code}.html"
    return {**parse_metadata(client.get(source)), "identity_source": source}


def annual(client, code, inception, end_year):
    source = f"https://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=yearzf&code={code}"
    return {"annual_returns": parse_annual(client.get(source), inception, end_year), "annual_source": source}
