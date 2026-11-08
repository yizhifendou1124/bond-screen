import calendar
import hashlib
import io
import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

from pypdf import PdfReader

from market_data import now, save_json


def compact(text):
    return re.sub(r"\s+", "", text)


def flat(text):
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    return re.sub(r"\s+", " ", text)


def declared_share_codes(text):
    text = flat(text)
    match = re.search(r"基金主代码|基金代码|交易代码", text)
    if not match:
        return set()
    passage = text[match.start():match.start() + 3500]
    end = re.search(r"主要财务指标|基金净值表现|管理人报告", passage)
    if end:
        passage = passage[:end.start()]
    return set(re.findall(r"(?<!\d)\d{6}(?!\d)", passage))


def period_end(title):
    numerals = str.maketrans("〇○零一二三四五六七八九", "000123456789")
    title = re.sub(r"[二2][〇○零0][二2][〇○零一二三四五六七八九0-9]", lambda match: match.group().translate(numerals), title)
    title = compact(title)
    if re.search(r"提示|摘要|公告", title):
        return None
    quarter = re.search(r"(20\d{2})年?第?([1-4一二三四])季度报告", title)
    interim = re.search(r"(20\d{2})年?(?:中期报告|半年度报告)", title)
    annual = re.search(r"(20\d{2})年?(?:年度报告|年报)", title)
    if quarter:
        year = int(quarter.group(1))
        quarter_text = quarter.group(2)
        month = (int(quarter_text) if quarter_text.isdigit() else "一二三四".index(quarter_text) + 1) * 3
    elif interim:
        year = int(interim.group(1))
        month = 6
    elif annual:
        year = int(annual.group(1))
        month = 12
    else:
        return None
    return date(year, month, calendar.monthrange(year, month)[1]).isoformat()


def announcements(client, code, kind, today, earliest=2020):
    result = {}
    total = None
    for page in range(1, 501):
        response = client.json("https://api.fund.eastmoney.com/f10/JJGG", {"fundCode": code, "pageIndex": page, "pageSize": 100, "type": kind})
        rows = response.get("Data")
        if not isinstance(rows, list):
            raise ValueError("公告列表结构改变")
        reported = response.get("TotalCount")
        if reported is not None:
            if total is not None and int(reported) != total:
                raise ValueError("公告分页期间总数变化")
            total = int(reported)
        for entry in rows:
            if not re.fullmatch(r"AN\d+", str(entry.get("ID", ""))) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(entry.get("PUBLISHDATEDesc", ""))):
                raise ValueError("公告编号或日期无效")
            if entry["ID"] in result:
                raise ValueError("公告分页重复")
            result[entry["ID"]] = entry
        if not rows or len(rows) < 100 or (total is not None and len(result) >= total):
            break
        if min(entry["PUBLISHDATEDesc"] for entry in rows) < f"{earliest}-01-01":
            break
    else:
        raise ValueError("公告超过分页安全上限")
    return sorted([entry for entry in result.values() if entry["PUBLISHDATEDesc"] <= today], key=lambda entry: (entry["PUBLISHDATEDesc"], entry["ID"]), reverse=True)


def document(client, entry, code, cache, refresh=False):
    identifier = entry["ID"]
    source = f"https://pdf.dfcfw.com/pdf/H2_{identifier}_1.pdf"
    cache = Path(cache)
    pdf_path = cache / f"{identifier}.pdf"
    metadata_path = cache / f"{identifier}.json"
    reused = False
    if not refresh and pdf_path.exists() and metadata_path.exists():
        content = pdf_path.read_bytes()
        metadata = json.loads(metadata_path.read_text())
        reused = metadata.get("sha256") == hashlib.sha256(content).hexdigest() and metadata.get("source") == source
    if not reused:
        content = client.get(source, binary=True)
    if not content.startswith(b"%PDF"):
        raise ValueError("公告附件不是PDF")
    reader = PdfReader(io.BytesIO(content))
    if not 1 <= len(reader.pages) <= 1000:
        raise ValueError("PDF页数异常")
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    if len(compact(text)) < 300:
        raise ValueError("PDF缺少可解析全文")
    cache.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    save_json(metadata_path, {"source": source, "sha256": digest})
    text_path = client.evidence / f"{identifier}.txt"
    text_path.write_text(text, encoding="utf-8")
    with client.lock:
        client.manifest.append({"url": source, "announcement_id": identifier, "checked_at": now().isoformat(), "sha256": digest, "immutable_pdf_reused_after_live_announcement_check": reused, "text_path": str(text_path.relative_to(client.evidence.parent))})
    return text, {"title": entry["TITLE"], "publication_date": entry["PUBLISHDATEDesc"], "source": f"https://fund.eastmoney.com/gonggao/{code},{identifier}.html", "pdf_source": source, "report_date": period_end(entry["TITLE"]), "sha256": digest}


def extract_scope(text):
    text = compact(text)
    candidates = []
    for match in re.finditer("投资范围", text):
        passage = text[match.start():match.start() + 10000]
        end = passage.find("投资策略")
        if not 40 < end < 9000 or not re.search(r"国债|金融债|债券|固定收益|标的指数", passage[:450]):
            continue
        passage = passage[:end]
        score = 10 if re.search(r"投资范围(?:本基金|主要为|包括|为)|本基金的投资范围", passage[:180]) else 0
        if re.search(r"基金净值|市场波动|特有风险", passage[:160]):
            score -= 10
        candidates.append((score, match.start(), passage))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    if not candidates or candidates[0][0] < 3:
        return "", None
    scope = candidates[0][2]
    reduced = re.sub(r"(?:可)?分离交易(?:可转换债券|可转债)(?:的|中(?:的)?)?纯债(?:券)?部分", "分离纯债", scope)
    for sentence in re.split(r"[。；;]", reduced):
        risks = list(re.finditer(r"股票|优先股|权证|可转换(?:公司)?债券|可转债|可交换(?:公司)?债|可转股|期货|衍生品|存托凭证|权益类|基金份额|其他基金", sentence))
        for risk in risks:
            prefix = sentence[:risk.start()]
            denied = re.search(r"(?:不(?:直接|主动)?(?:投资|买入|购买|参与|进行)|不包括|不得投资|不可投资)[^，,但]{0,60}$", prefix)
            if denied and not re.search(r"因.*转股", sentence):
                continue
            return scope, None if "未来" in sentence or "以后" in sentence else False
    return scope, True


def amount(text):
    return Decimal(0) if text in "-—－–" else Decimal(text.replace(",", ""))


def portfolio(text):
    text = flat(text)
    asset_index = max(text.rfind("期末基金资产组合情况"), text.rfind("报告期末基金资产组合"))
    bond_index = text.rfind("按债券品种分类的债券投资组合")
    asset = text[asset_index:asset_index + 2500] if asset_index >= 0 else ""
    bond = text[bond_index:bond_index + 1600] if bond_index >= 0 else ""
    fields = {}
    patterns = [("equity", "权益投资", asset), ("stock", "股票", asset), ("fund", "基金投资", asset), ("precious_metals", "贵金属投资", asset), ("derivatives", "金融衍生品投资", asset), ("convertible_exchangeable", r"(?:可转债\s*[（(]\s*可交换债\s*[）)]|可转换债券(?:\s*[（(]\s*可交换债\s*[）)])?)", bond)]
    for label, pattern, passage in patterns:
        match = re.search(pattern + r"\s+([-—－–]|[\d,]+\.\d+)", passage)
        fields[label] = amount(match.group(1)) if match else None
    if fields["fund"] is None:
        components = []
        for label in ["固定收益投资", "买入返售金融资产", "银行存款和结算备付金合计", "其他(?:各项)?资产"]:
            match = re.search(label + r"\s+([-—－–]|[\d,]+\.\d+)", asset)
            components.append(amount(match.group(1)) if match else None)
        total = re.search(r"\b\d+\s+合计\s+([\d,]+\.\d+)\s+100[.]00", asset)
        known = [fields[label] for label in ["equity", "precious_metals", "derivatives"]]
        if total and all(value is not None for value in components + known) and sum(components + known) == amount(total.group(1)):
            fields["fund"] = Decimal(0)
    positive = [key for key, value in fields.items() if value is not None and value > 0]
    missing = [key for key, value in fields.items() if value is None]
    return {"status": "non_pure" if positive else "unverified" if missing else "clear", "amounts_cny": {key: str(value) if value is not None else None for key, value in fields.items()}, "positive": positive, "missing": missing, "asset_table": asset[:1800], "bond_table": bond[:1200]}


def parse_assets(text, full_name, report_date):
    if compact(full_name) not in compact(text):
        raise ValueError("最新报告基金全称与身份不一致")
    if not report_date:
        raise ValueError("报告截止日未确认")
    chinese_date = f"{int(report_date[:4])}年{int(report_date[5:7])}月{int(report_date[8:])}日"
    if report_date not in compact(text) and chinese_date not in compact(text):
        padded_date = report_date.replace("-", "年", 1).replace("-", "月") + "日"
        if padded_date not in compact(text):
            raise ValueError("报告截止日未在全文中确认")
    text = flat(text)
    start = text.find("期末基金资产净值")
    if start < 0:
        raise ValueError("整只基金净资产行缺失")
    tail = text[start + len("期末基金资产净值"):start + 1100]
    end = re.search(r"(?:\d[.．、]\s*)?期末基金份额净值", tail)
    if not end:
        raise ValueError("净资产行终点未确认")
    row = re.sub(r"\s+\d+\s*[.．、]\s*$", "", tail[:end.start()])
    row = re.sub(r"(\.\d?)\s+(\d{1,2})(?!\d)", r"\1\2", row)
    values = re.findall(r"\d[\d,]*\.\d{2}\b", row)
    residual = re.sub(r"\d[\d,]*\.\d{2}\b", "", row).strip()
    before = text[max(0, start - 1000):start]
    if not re.search(r"(?:金额)?单位\s*[:：]?\s*(?:人民币元|元\s*人民币|人民币\s*[（(]元[）)])", before):
        raise ValueError("净资产表人民币单位未确认，不能把外币金额当作人民币规模")
    aggregate = bool(re.search(r"基金合计|全基金|所有份额合计", before))
    share_codes = declared_share_codes(text)
    if len(share_codes) > len(values) and not aggregate:
        raise ValueError("净资产行未覆盖已披露全部份额，不能采用单份额规模")
    if not values or residual or len(values) > 12 or (len(values) > 1 and aggregate):
        raise ValueError(f"全份额净资产行存在歧义: {row[:180]}")
    return {"fund_assets_cny": str(sum(amount(value) for value in values)), "fund_assets_date": report_date, "fund_assets_basis": "fund_total", "net_assets_row": row, "components_cny": values}


def other_assets_clear(text):
    text = flat(text)
    index = text.rfind("其他各项资产构成")
    passage = text[index:index + 1800] if index >= 0 else ""
    rows = re.findall(r"\b\d+\s+([\u4e00-\u9fff]+)\s+([-—－–]|[\d,]+\.\d+)", passage)
    allowed = {"存出保证金", "应收证券清算款", "应收清算款", "应收利息", "应收申购款", "其他应收款", "待摊费用"}
    totals = [amount(value) for label, value in rows if label == "合计"]
    components = [(label, amount(value)) for label, value in rows if label != "合计"]
    return bool(len(totals) == 1 and components and sum(value for _, value in components) == totals[0] and all(value == 0 or label in allowed for label, value in components))


def stock_trades_clear(text):
    text = compact(text)
    index = text.rfind("买入股票的成本总额及卖出股票的收入总额")
    passage = text[index:index + 800] if index >= 0 else ""
    return bool(re.search(r"未进行股票交易|无股票交易|未投资股票|未买入股票|未进行股票投资|收入总额无[。.]", passage))


def permitted_non_stock_trades_clear(text, scope):
    reduced = re.sub(r"(?:可)?分离交易(?:可转换债券|可转债)(?:的|中(?:的)?)?纯债(?:券)?部分", "分离纯债", compact(scope))
    text = compact(text)
    findings = []
    for sentence in re.split(r"[。；;]", reduced):
        for risk in re.finditer(r"优先股|权证|可转换(?:公司)?债券|可转债|可交换(?:公司)?债|可转股|期货|衍生品|存托凭证|权益类|基金份额|其他基金", sentence):
            if re.search(r"(?:不(?:直接|主动)?(?:投资|买入|购买|参与|进行)|不包括|不得投资|不可投资)[^，,但]{0,60}$", sentence[:risk.start()]):
                continue
            instrument = re.escape(risk.group())
            evidence = re.search(r"(?:本报告期(?!末)|报告期内|本期(?!末))[^。；，,但]{0,60}(?:未(?:曾)?(?:投资|买入|持有|参与|进行)|没有(?:投资|买入|持有|参与))[^。；，,但]{0,35}" + instrument, text)
            findings.append(bool(evidence))
    return all(findings)


def yearly_quote(text, name, single_class):
    text = flat(text)
    letter = re.search(r"([A-Z])(?:类)?(?:[（(][^）)]*[）)])?$", name)
    quotes = []
    for match in re.finditer(r"过去一年\s*(-?\d+(?:\.\d+)?)\s*%", text):
        context = text[max(0, match.start() - 2200):match.start()]
        labels = re.findall(r"(?:[\u4e00-\u9fff]{2,})([A-Z])(?:类)?|\b([A-Z])\s*类", context)
        labels = [label[0] or label[1] for label in labels]
        if single_class or (letter and labels and labels[-1] == letter.group(1)):
            quotes.append(Decimal(match.group(1)))
    return quotes[0] if len(quotes) == 1 else None


def event_findings(text, year, source):
    text = compact(text)
    findings = []
    for match in re.finditer(r"大额赎回|巨额赎回|净值异常|净值更正|净值差错", text):
        passage = text[max(0, match.start() - 120):match.start() + 200]
        effect = re.search(r"(?:导致|致使|使得|影响|造成|因).{0,70}(?:净值(?:增长|上涨|增长率)|收益率|净值大幅)|净值异常|净值更正|净值差错", passage)
        if effect and not re.search(r"未发生|不存在|未出现", passage):
            findings.append({"year": year, "reason": "报告披露疑似非经常性净值事件，需要核实影响", "sources": [source], "excerpt": passage})
    return findings


def review_family(client, funds, cache, today, as_of, earliest=2020):
    representative = min(funds, key=lambda fund: (not fund["name"].endswith("A"), fund["inception_date"], fund["code"]))
    code = representative["code"]
    full_name = representative["full_name"]
    periodic = announcements(client, code, 3, today, earliest)
    general = announcements(client, code, 0, today, earliest)
    available = {entry["ID"]: entry for entry in periodic + general}
    reports = [entry for entry in available.values() if period_end(entry["TITLE"]) and period_end(entry["TITLE"]) <= as_of]
    if not reports:
        raise ValueError("最新定期报告未取得")
    latest = max(reports, key=lambda entry: (period_end(entry["TITLE"]), entry["PUBLISHDATEDesc"], entry["ID"]))
    text, latest_doc = document(client, latest, code, cache, refresh=True)
    if compact(full_name) not in compact(text):
        raise ValueError("管理人报告身份不一致")
    declared_codes = declared_share_codes(text)
    result = {"representative": code, "latest_report": latest_doc, "verified_codes": sorted(declared_codes.intersection(fund["code"] for fund in funds)), "unverified_share_codes": sorted(declared_codes.difference(fund["code"] for fund in funds)), "pure_bond": None, "pure_bond_basis": "contract", "annual_anomaly_review": {"status": "unverified", "years": [], "sources": [], "findings": []}}
    holdings = portfolio(text)
    result["holdings"] = holdings
    if holdings["status"] == "non_pure":
        result["pure_bond"] = False
        return result
    try:
        result.update(parse_assets(text, full_name, latest_doc["report_date"]))
        result["fund_assets_sources"] = [latest_doc["source"], latest_doc["pdf_source"]]
    except ValueError as error:
        result["size_error"] = str(error)
    prospectuses = [entry for entry in available.values() if "招募说明书" in entry["TITLE"] and not re.search(r"提示|概要|摘要|关于|公告$", entry["TITLE"])]
    if not prospectuses:
        result["purity_error"] = "最新完整招募说明书未取得"
        return result
    prospectus = max(prospectuses, key=lambda entry: (entry["PUBLISHDATEDesc"], entry["ID"]))
    scope_text, scope_doc = document(client, prospectus, code, cache, refresh=True)
    if compact(full_name) not in compact(scope_text):
        raise ValueError("最新招募说明书身份不一致")
    scope, contract_pure = extract_scope(scope_text)
    result["prospectus"] = {**scope_doc, "scope": scope, "contract_pure": contract_pure}
    later = [entry for entry in available.values() if entry["PUBLISHDATEDesc"] > latest_doc["publication_date"] and re.search(r"投资范围|基金合同|转型|合并|终止|清算|收益异常|净值更正", entry["TITLE"]) and entry["ID"] != prospectus["ID"]]
    result["later_relevant_announcements"] = later
    if holdings["status"] == "clear" and scope and not later:
        if contract_pure is True:
            result["pure_bond"] = True
        else:
            interim_reports = [entry for entry in reports if re.search(r"中期报告|半年度报告|年度报告", entry["TITLE"])]
            if interim_reports:
                interim_entry = max(interim_reports, key=lambda entry: (period_end(entry["TITLE"]), entry["PUBLISHDATEDesc"]))
                interim_text, interim_doc = document(client, interim_entry, code, cache, refresh=interim_entry["ID"] != latest["ID"])
                if compact(full_name) not in compact(interim_text):
                    raise ValueError("交易报告身份不一致")
                clear = portfolio(interim_text)["status"] == "clear" and stock_trades_clear(interim_text) and permitted_non_stock_trades_clear(interim_text, scope) and other_assets_clear(text) and other_assets_clear(interim_text)
                result["pure_bond"] = True if clear else None
                result["pure_bond_basis"] = "actual_holdings"
                result["pure_bond_review"] = {"report_date": latest_doc["report_date"], "checked_on": today, "coverage_complete": clear, "non_pure_assets_held": False if clear else None, "sources": [latest_doc["source"], scope_doc["source"], interim_doc["source"]], "findings": ["完整资产组合、债券品种、其他资产、最近半年/全年股票交易及合同允许的其他非纯债资产投资情况交叉核查。", f"合同许可范围：{scope}；仅代表最新披露，持仓有滞后。"]}
    reviewed_years = []
    sources = []
    clues = []
    confirmed = []
    quote_checks = []
    for year in range(earliest, int(as_of[:4])):
        annual_entries = [entry for entry in reports if period_end(entry["TITLE"]) == f"{year}-12-31" and re.search(r"年度报告|年报", entry["TITLE"])]
        if not annual_entries:
            quote_checks.append({"year": year, "status": "missing_official_annual_report"})
            continue
        entry = max(annual_entries, key=lambda item: (item["PUBLISHDATEDesc"], item["ID"]))
        annual_text, annual_doc = document(client, entry, code, cache)
        if compact(full_name) not in compact(annual_text):
            raise ValueError("历史年度报告身份不一致")
        sources.append(annual_doc["source"])
        events = event_findings(annual_text, year, annual_doc["source"])
        clues.extend(events)
        for event in events:
            passage = event["excerpt"]
            if re.search(r"(?:本报告期|本期|报告期内).{0,40}(?:发生|出现|遭遇).{0,40}(?:大额|巨额)赎回.{0,80}(?:导致|致使|造成).{0,40}净值(?:大幅)?(?:增长|上涨)", passage) and not re.search(r"可能|如果|当发生", passage):
                confirmed.append({key: event[key] for key in ("year", "reason", "sources")})
        checks = []
        annual_codes = declared_share_codes(annual_text)
        active_classes = [fund for fund in funds if fund["inception_date"] <= f"{year}-01-01" and fund["code"] in annual_codes]
        for fund in funds:
            expected = fund["annual_returns"].get(str(year))
            if expected is None:
                continue
            quote = yearly_quote(annual_text, fund["name"], len(active_classes) == 1 and fund["code"] in annual_codes)
            matched = quote is not None and abs(quote - Decimal(expected)) <= Decimal("0.02") and (quote < 0) == (Decimal(expected) < 0)
            checks.append({"code": fund["code"], "expected": expected, "official_quote": str(quote) if quote is not None else None, "matched": matched})
        quote_checks.append({"year": year, "checks": checks, "source": annual_doc["source"]})
        if checks and all(check["matched"] for check in checks) and not events:
            reviewed_years.append(year)
    result["annual_quote_checks"] = quote_checks
    result["annual_event_clues"] = clues
    complete = reviewed_years == list(range(earliest, int(as_of[:4]))) and not result["unverified_share_codes"]
    result["annual_anomaly_review"] = {"status": "confirmed" if confirmed else "clear" if complete and not clues else "unverified", "years": reviewed_years, "sources": sources, "findings": confirmed}
    return result
