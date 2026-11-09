import calendar
import hashlib
import io
import json
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from pypdf import PdfReader, __version__ as pdf_version
from pypdf.errors import PyPdfError

from market_data import now, save_json
from market_cache import fingerprint


def compact(text):
    return re.sub(r"\s+", "", text)


def flat(text):
    text = text.replace("％", "%").replace("−", "-")
    text = re.sub(r"(?<=[.,])\s+(?=\d)|(?<=\d)\s+(?=[.,])", "", text)
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    return re.sub(r"\s+", " ", text)


def restricted_scope(text):
    text = compact(text)
    text = re.sub(r"(?:可)?分离交易(?:可转换债券|可转债)(?:的|中(?:的)?)?纯债(?:券)?部分", "分离纯债", text)
    return text.replace("国债期货", "国债利率工具").replace("基金份额持有人", "持有人")


def treasury_futures_review(text, scope):
    normalized = compact(text)
    required = "国债期货" in compact(scope) or "国债期货" in normalized
    result = {"required": required, "status": "unverified" if required else "not_applicable", "positions": [], "checks": {}, "findings": []}
    if not required:
        return result
    no_trades_pattern = r"(?:本报告期(?!末)|报告期内|本期(?!末|国债期货投资政策))(?:(?!期末)[^。；但]){0,25}(?:未(?:进行|参与)?(?:投资|交易)(?:于)?国债期货|未(?:进行|参与)国债期货(?:投资|交易)|无国债期货(?:投资|交易))(?=[。；，]|$)"

    def period_absence(passage):
        return next((sentence for sentence in re.split(r"[。；]", passage) if re.search(no_trades_pattern, sentence) and not re.search(r"如果|若|可能|假设", sentence)), None)

    headings = list(re.finditer(r"(?<![\d.])(\d+)\.(\d+)(?:报告期末|期末)?(?:本基金)?(?:投资的)?国债期货交易情况说明", normalized))
    if not headings:
        no_trades = period_absence(normalized)
        if no_trades:
            result.update(status="clear", checks={"no_period_investment": True}, excerpt=no_trades)
            return result
        result["findings"].append("未取得可识别的国债期货专门披露；期末为零不能替代用途及期内交易核查")
        return result
    heading = headings[-1]
    next_section = rf"(?<![\d.]){heading.group(1)}\.{int(heading.group(2)) + 1}(?![\d.])"
    end = re.search(next_section + r"(?=[\u4e00-\u9fff])", normalized[heading.end():])
    section = normalized[heading.start():heading.end() + end.start() if end else heading.start() + 12000]
    readable = flat(text)
    start = readable.rfind("国债期货交易情况说明")
    readable_section = readable[start:start + 14000] if start >= 0 else ""
    next_heading = re.search(next_section + r"\s*(?=[\u4e00-\u9fff])", readable_section[len("国债期货交易情况说明"):])
    if next_heading:
        readable_section = readable_section[:len("国债期货交易情况说明") + next_heading.start()]
    table = re.search(r"国债期货持仓和损益明细(.*?)(?:公允价值变动总额合计|国债期货投资本期收益|本期国债期货投资评价|$)", readable_section)
    rows = []
    codes = set()
    if table:
        codes = set(re.findall(r"\b([A-Z]{1,3}\d{4})\b", table.group(1)))
        pattern = r"\b([A-Z]{1,3}\d{4})\b(.{0,160}?)\s(-?[\d,]+)\s+(-?[\d,]+\.\d{2})\s+(-?[\d,]+\.\d{2})(?=\s|$)"
        for match in re.finditer(pattern, table.group(1)):
            rows.append({"code": match.group(1), "name": match.group(2).strip(), "quantity": str(amount(match.group(3))), "contract_notional_cny": str(amount(match.group(4))), "fair_value_change_cny": str(amount(match.group(5)))})
    result["positions"] = rows
    known = all(re.fullmatch(r"(?:T|TF|TS|TL)\d{4}", row["code"]) and "国债期货" in compact(row["name"]) for row in rows)
    equity_positions = [row for row in rows if re.fullmatch(r"(?:IF|IH|IC|IM)\d{4}", row["code"]) and Decimal(row["quantity"]) != 0]
    no_end_positions = bool(re.search(r"(?:本报告期末|报告期末|期末)[^。；但]{0,25}(?:未(?:持有|投资)国债期货|无国债期货(?:投资|持仓))(?=[。；，]|$)", section))
    empty_positions = bool(re.search(r"国债期货持仓和损益明细(?:注[:：])?无[。.]?(?=\d+\.\d+|$)", section))
    sentences = re.split(r"[。；]", section)
    purpose = any(re.search(r"以套期保值为(?:主要)?目的", sentence) and not re.search(r"(?:不|非|未)以套期保值|不是以套期保值", sentence) for sentence in sentences)
    management = any(re.search(r"(?:国债期货|期货交易)[^。；]{0,40}(?:管理|对冲|降低|调节|调整)[^。；]{0,25}(?:利率风险|久期|期限结构)", sentence) and not re.search(r"如果|若|可能|拟|未来", sentence) for sentence in sentences)
    no_trades = period_absence(section)
    nonzero_positions = any(Decimal(row["quantity"]) != 0 for row in rows)
    consistent = (not no_trades or not (management or nonzero_positions)) and not (no_end_positions and nonzero_positions)
    permission = "国债期货" in compact(scope) and not re.search(r"(?:不投资(?:于)?|不参与|不得投资|不可投资)[^。；，]{0,15}国债期货", compact(scope))
    positions_disclosed = (no_end_positions and not rows and not codes) or bool(rows and known and codes == {row["code"] for row in rows})
    checks = {"contract_permits_treasury_futures": permission, "hedging_purpose": purpose, "interest_rate_management": management, "positions_disclosed": positions_disclosed, "period_disclosure_consistent": consistent}
    result["checks"] = checks
    result["excerpt"] = section
    activity = bool(re.search(r"套期保值|套保|投机|套利", section)) or any(re.search(r"(?:运用|参与|进行|交易|买入|卖出|投资)(?:了)?国债期货", sentence) and not re.search(r"(?:未|不|没有|无)(?:曾|进行|参与)?(?:运用|参与|进行|交易|买入|卖出|投资)?国债期货", sentence) for sentence in re.split(r"[。；，]|但", section))
    profit_quotes = re.findall(r"(?:公允价值变动总额合计|国债期货投资本期(?:收益|公允价值变动))(?:[（(]元[）)])?[:：]?(-?[\d,]+\.\d+)", section)
    period_profit = any(amount(quote) != 0 for quote in profit_quotes)
    if (no_end_positions or empty_positions) and not rows and not codes and not activity and not period_profit:
        result.update(required=False, status="not_applicable")
        result["findings"].append("报告仅有期末无持仓的模板披露，未识别实际使用线索；不把允许的国债期货或章节标题当作非纯债敞口，也不将期末为零宣称为全期无交易")
        return result
    if equity_positions:
        result["status"] = "non_pure"
        result["findings"].append("持仓明细存在股指期货实际敞口，不能借国债期货核查豁免")
    elif no_trades and consistent:
        result.update(status="clear", checks={"no_period_investment": True}, excerpt=no_trades)
    elif all(checks.values()):
        result["status"] = "clear"
        notionals = [Decimal(row["contract_notional_cny"]) for row in rows]
        result["gross_contract_notional_cny"] = str(sum((abs(value) for value in notionals), Decimal(0)))
        result["net_contract_notional_cny"] = str(sum(notionals, Decimal(0)))
        result["findings"].append("披露用途为套保及利率／久期管理，持仓明细已核对；名义市值不等于久期风险敞口，不保证套保有效，仍有杠杆及基差风险")
    else:
        result["findings"].append("国债期货用途、品种或持仓披露尚未完整核实，不因交易事实直接判为非纯债")
    return result


def declared_share_codes(text):
    text = flat(text)
    codes = set()
    for match in re.finditer(r"(?:基金主代码|基金代码|交易代码)\s*[:：]?\s*([\d\s]+)", text):
        digits = compact(match.group(1))
        if len(digits) % 6 == 0:
            codes.update(digits[index:index + 6] for index in range(0, len(digits), 6))
    return codes


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
    cached_text_path = cache / f"{identifier}.txt"
    timestamp = now()
    metadata = {}
    reused = False
    if not getattr(client, "force_refresh", False) and pdf_path.exists() and metadata_path.exists():
        try:
            content = pdf_path.read_bytes()
            metadata = json.loads(metadata_path.read_text())
            age = timestamp - datetime.fromisoformat(metadata["downloaded_at"])
            reused = metadata.get("sha256") == hashlib.sha256(content).hexdigest() and metadata.get("source") == source and timedelta(0) <= age <= timedelta(days=7)
        except (ValueError, TypeError, KeyError, OSError):
            reused = False
    if not reused:
        content = client.get(source, binary=True)
    if not content.startswith(b"%PDF"):
        raise ValueError("公告附件不是PDF")
    digest = hashlib.sha256(content).hexdigest()
    parser_version = fingerprint([Path(__file__).read_text(), pdf_version])
    text_reused = False
    if reused and metadata.get("parser_version") == parser_version and cached_text_path.exists():
        try:
            text = cached_text_path.read_text(encoding="utf-8")
            text_reused = metadata.get("text_sha256") == hashlib.sha256(text.encode()).hexdigest()
        except (OSError, UnicodeError):
            text_reused = False
    if not text_reused:
        reader = PdfReader(io.BytesIO(content))
        if not 1 <= len(reader.pages) <= 1000:
            raise ValueError("PDF页数异常")
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    if len(compact(text)) < 300:
        raise ValueError("PDF缺少可解析全文")
    cache.mkdir(parents=True, exist_ok=True)
    downloaded_at = metadata["downloaded_at"] if reused else timestamp.isoformat()
    if not reused:
        pdf_path.write_bytes(content)
    if not text_reused:
        cached_text_path.write_text(text, encoding="utf-8")
    save_json(metadata_path, {"source": source, "sha256": digest, "downloaded_at": downloaded_at, "parser_version": parser_version, "text_sha256": hashlib.sha256(text.encode()).hexdigest()})
    text_path = client.evidence / f"{identifier}.txt"
    text_path.write_text(text, encoding="utf-8")
    with client.lock:
        client.manifest.append({"url": source, "announcement_id": identifier, "checked_at": timestamp.isoformat(), "original_fetched_at": downloaded_at, "sha256": digest, "immutable_pdf_reused_after_live_announcement_check": reused, "extracted_text_reused": text_reused, "text_path": str(text_path.relative_to(client.evidence.parent))})
    data_cache = getattr(client, "data_cache", None)
    if data_cache is not None:
        data_cache.event("pdf", identifier, "reused" if reused else "updated", original_fetched_at=downloaded_at, sha256=digest)
        data_cache.event("pdf_text", identifier, "reused" if text_reused else "updated", sha256=hashlib.sha256(text.encode()).hexdigest())
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
        heading = text[max(0, match.start() - 20):match.start()]
        if re.search(r"(?:[一二三四五六七八九十]+[、）)]|\d+[、.])$", heading):
            score += 20
        if re.search(r"目录|第[一二三四五六七八九十]+部分释义", passage):
            score -= 20
        if re.search(r"基金净值|市场波动|特有风险", passage[:160]):
            score -= 10
        candidates.append((score, match.start(), passage))
    candidates.sort(key=lambda item: (-item[0], len(item[2]), item[1]))
    if not candidates or candidates[0][0] < 3:
        return "", None
    scope = candidates[0][2]
    reduced = restricted_scope(scope)
    for sentence in re.split(r"[。；;]", reduced):
        risks = list(re.finditer(r"股票|优先股|权证|可转换(?:公司)?债券|可转债|可交换(?:公司)?债|可转股|期货|衍生品|存托凭证|权益类|基金份额|其他基金", sentence))
        for risk in risks:
            prefix = sentence[:risk.start()]
            denied = re.search(r"(?:不(?:直接|主动)?(?:投资(?:于)?|买入|购买|参与|进行)|不包括|不得投资|不可投资)[^，,但]{0,60}$", prefix)
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
    positive = [key for key, value in fields.items() if key != "derivatives" and value is not None and value > 0]
    missing = [key for key, value in fields.items() if value is None]
    if fields["derivatives"] is not None and fields["derivatives"] != 0:
        missing.append("derivatives_instrument_and_amount_unconfirmed")
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
    index = max(text.rfind("其他各项资产构成"), text.rfind("其他资产构成"))
    passage = text[index:index + 6000] if index >= 0 else ""
    total = re.search(r"\b\d+\s+合计\s+([-—－–]|[\d,]+\.\d+)", passage)
    passage = passage[:total.end()] if total else ""
    rows = re.findall(r"\b\d+\s+([\u4e00-\u9fff]+)\s+([-—－–]|[\d,]+\.\d+)", passage)
    allowed = {"存出保证金", "应收证券清算款", "应收清算款", "应收利息", "应收申购款", "其他应收款", "待摊费用"}
    totals = [amount(value) for label, value in rows if label == "合计"]
    components = [(label, amount(value)) for label, value in rows if label != "合计"]
    return bool(len(totals) == 1 and components and sum(value for _, value in components) == totals[0] and all(value == 0 or label in allowed for label, value in components))


def stock_trades_clear(text):
    text = compact(text)
    heading = "买入股票的成本总额及卖出股票的收入总额"
    index = text.rfind(heading)
    passage = text[index:index + 800] if index >= 0 else ""
    passage = re.split(r"\d+\.\d+(?:\.\d+)?(?=[\u4e00-\u9fff])", passage, maxsplit=1)[0]
    if re.fullmatch(r"注[:：]无[。.]?", passage[len(heading):]):
        return True
    return bool(re.search(r"(?:本报告期(?!末)|报告期内|本期(?!末))[^。；但]{0,25}(?:未(?:曾)?(?:持有|投资|买入(?:[、及和]?卖出)?)股票|无股票投资)|未进行股票交易|无股票交易|未进行股票投资|收入总额无[。.]", passage))


def historical_identity(text, full_name, prospectus_text, verified_codes):
    normalized = compact(text)
    if compact(full_name) in normalized:
        return {"verified": True, "basis": "current_name"}
    declared = declared_share_codes(text)
    if not declared or not declared.intersection(verified_codes) or not declared.issubset(verified_codes):
        return {"verified": False, "basis": "identity_unconfirmed"}
    names = re.findall(r"《([^《》]{4,90}?)基金合同》已?于\d{4}年\d{1,2}月\d{1,2}日(?:正式)?生效", compact(prospectus_text))
    for name in names:
        if name in normalized:
            return {"verified": True, "basis": "current_prospectus_original_contract_and_codes", "historical_name": name, "declared_codes": sorted(declared)}
    return {"verified": False, "basis": "identity_unconfirmed"}


def review_later_announcements(client, entries, code, cache, full_name, today, contract_pure):
    reviews = []
    for entry in sorted(entries, key=lambda item: (item["PUBLISHDATEDesc"], item["ID"])):
        record = {"announcement_id": entry["ID"], "publication_date": entry["PUBLISHDATEDesc"], "status": "unverified"}
        try:
            text, source = document(client, entry, code, cache, refresh=True)
            record["source"] = source["source"]
            normalized = compact(text)
            if compact(full_name) not in normalized or re.search(r"转型|合并|终止|清算|收益异常|净值更正|投资范围", entry["TITLE"]):
                record["finding"] = "基金身份或公告影响需进一步核实"
            elif "公告" in entry["TITLE"] and "业绩比较基准" in entry["TITLE"]:
                effective = re.search(r"修订后的基金合同[^。；]{0,120}自(20\d{2})年(\d{1,2})月(\d{1,2})日起生效", normalized)
                if effective:
                    record["effective_date"] = date(*(int(value) for value in effective.groups())).isoformat()
                    if record["effective_date"] > today and contract_pure is True:
                        record["status"] = "future_change_not_yet_effective"
            elif "基金合同" in entry["TITLE"] and "公告" not in entry["TITLE"]:
                scope, pure = extract_scope(text)
                record.update(updated_scope=scope, updated_contract_pure=pure)
        except (RuntimeError, ValueError, PyPdfError, UnicodeError, OSError) as error:
            record["finding"] = str(error)
        reviews.append(record)
    future_dates = {record["publication_date"]: record["effective_date"] for record in reviews if record["status"] == "future_change_not_yet_effective"}
    for record in reviews:
        if record.get("updated_contract_pure") is True and record["publication_date"] in future_dates and contract_pure is True:
            record.update(status="future_pure_contract_checked", effective_date=future_dates[record["publication_date"]])
    return reviews


def permitted_non_stock_trades_clear(text, scope):
    reduced = restricted_scope(scope)
    text = compact(text)
    findings = []
    for sentence in re.split(r"[。；;]", reduced):
        for risk in re.finditer(r"优先股|权证|可转换(?:公司)?债券|可转债|可交换(?:公司)?债|可转股|期货|衍生品|存托凭证|权益类|基金份额|其他基金", sentence):
            if re.search(r"(?:不(?:直接|主动)?(?:投资(?:于)?|买入|购买|参与|进行)|不包括|不得投资|不可投资)[^，,但]{0,60}$", sentence[:risk.start()]):
                continue
            instrument = re.escape(risk.group())
            evidence = re.search(r"(?:本报告期(?!末)|报告期内|本期(?!末))[^。；，,但]{0,60}(?:未(?:曾)?(?:投资|买入|持有|参与|进行)|没有(?:投资|买入|持有|参与)|无)[^。；，,但]{0,35}" + instrument + r"(?:券)?(?:投资|交易|持仓)?(?=[。；，,、]|及|和|$)", text)
            findings.append(bool(evidence))
    return all(findings)


def non_pure_period_trades(text):
    findings = []
    normalized = restricted_scope(text)
    period = r"(?:本报告期内|本报告期(?!末)|报告期内|本期(?!末))"
    actor = rf"(?:本基金(?:在|于)?{period}|{period}[，,]?(?:本基金)?)"
    pattern = actor + r"(?:曾|实际|累计|已|共|择机|先后|进行)?(?:买入|卖出|持有|交易|参与|投资(?!范围|组合|比例|策略|政策|目标|品种))[^。；，]{0,20}(股票|权证|可转换(?:公司)?债券|可转债|可交换(?:公司)?债|股指期货|权益类基金)"
    for sentence in re.split(r"[。；]", normalized):
        for match in re.finditer(pattern, sentence):
            prefix = re.split(r"[，,]|但", sentence[:match.start()])[-1]
            if re.search(r"如果|若|可能|假设", prefix) or re.search(r"(?:未曾|未|没有|不得|不可|不允许|不能|允许|可以|计划|拟)$", prefix):
                continue
            findings.append({"instrument": match.group(1), "excerpt": match.group()})
    return findings


def share_labels(text):
    matches = re.finditer(r"(?:[\u4e00-\u9fff]{2,}\s*([A-Z])(?:\s*类)?|(?<![A-Za-z])([A-Z])\s*类)(?![A-Za-z])", text)
    return [match.group(1) or match.group(2) for match in matches]


def yearly_quote(text, name, single_class, year=None):
    text = flat(text)
    letter = re.search(r"([A-Z])(?:类)?(?:[（(][^）)]*[）)])?$", compact(name))
    quotes = []
    for match in re.finditer(r"(?:过去一年|过去1年)\s*(-?\d+(?:\.\d+)?)\s*%", text):
        context = text[max(0, match.start() - 2200):match.start()]
        labels = share_labels(context)
        if single_class or (letter and labels and labels[-1] == letter.group(1)):
            quotes.append(Decimal(match.group(1)))
    if quotes:
        return quotes[0] if len(set(quotes)) == 1 else None
    if year is None:
        return None
    for match in re.finditer("本期基金份额净值增长率", text):
        context = text[max(0, match.start() - 6000):match.start()]
        section = context.rfind("主要财务指标")
        if section < 0:
            continue
        context = context[section:]
        years = re.findall(r"(20\d{2})\s*年", context)
        if not years or int(years[0]) != year:
            continue
        labels = list(dict.fromkeys(share_labels(context)))
        row = re.match(r"(?:\s*-?\d+(?:\.\d+)?\s*%)+", text[match.end():])
        values = re.findall(r"(-?\d+(?:\.\d+)?)\s*%", row.group() if row else "")
        if single_class and not labels and values:
            quotes.append(Decimal(values[0]))
        elif letter and letter.group(1) in labels and len(values) >= len(labels) and len(values) % len(labels) == 0:
            quotes.append(Decimal(values[labels.index(letter.group(1))]))
    return quotes[0] if quotes and len(set(quotes)) == 1 else None


def event_findings(text, year, source):
    text = compact(text)
    findings = []
    for passage in re.split(r"[。；;！？!?]", text):
        if not re.search(r"大额赎回|巨额赎回|净值异常|净值更正|净值差错", passage):
            continue
        occurred = re.search(r"(?:^|[，,])(?:本基金)?(?:本报告期|本期|报告期内)[^，,]{0,35}(?:发生|出现|遭遇)[^，,]{0,35}(?:大额|巨额)赎回(?!风险)", passage)
        if not occurred and re.search(r"可能|如果|一旦|若发生|当.{0,80}时|赎回风险", passage):
            continue
        affirmative = re.sub(r"(?:未发生|不存在|未出现)[^，,]{0,40}(?:大额赎回|巨额赎回|净值异常|净值更正|净值差错)", "", passage)
        effect = re.search(r"(?:导致|致使|使得|影响|造成|因).{0,70}(?:净值(?:增长|上涨|增长率)|收益率|净值大幅)|净值异常|净值更正|净值差错", affirmative)
        if effect and re.search(r"大额赎回|巨额赎回|净值异常|净值更正|净值差错", affirmative):
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
    result = {"representative": code, "latest_report": latest_doc, "verified_codes": sorted(declared_codes.intersection(fund["code"] for fund in funds)), "unverified_share_codes": sorted(declared_codes.difference(fund["code"] for fund in funds)), "pure_bond": None, "pure_bond_basis": "contract", "annual_anomaly_review": {"status": "unverified", "years": [], "sources": [], "findings": []}, "verification_issues": [], "disclosure_notes": [], "annual_return_conflicts": {}}
    issues = result["verification_issues"]
    holdings = portfolio(text)
    result["holdings"] = holdings
    if holdings["status"] == "unverified":
        issues.append({"kind": "holdings_unparsed", "message": "完整组合表部分字段未识别，不代表实际持有非纯债资产", "components": holdings["missing"], "source": latest_doc["source"]})
    if holdings["status"] == "non_pure":
        result["pure_bond"] = False
        return result
    try:
        result.update(parse_assets(text, full_name, latest_doc["report_date"]))
        result["fund_assets_sources"] = [latest_doc["source"], latest_doc["pdf_source"]]
    except ValueError as error:
        result["size_error"] = str(error)
        issues.append({"kind": "assets_unparsed", "message": str(error), "source": latest_doc["source"]})
    prospectuses = [entry for entry in available.values() if "招募说明书" in entry["TITLE"] and not re.search(r"提示|概要|摘要|关于|公告$", entry["TITLE"])]
    if not prospectuses:
        result["purity_error"] = "最新完整招募说明书未取得"
        issues.append({"kind": "source_unavailable", "message": result["purity_error"]})
        return result
    prospectus = max(prospectuses, key=lambda entry: (entry["PUBLISHDATEDesc"], entry["ID"]))
    scope_text, scope_doc = document(client, prospectus, code, cache, refresh=True)
    if compact(full_name) not in compact(scope_text):
        raise ValueError("最新招募说明书身份不一致")
    scope, contract_pure = extract_scope(scope_text)
    result["prospectus"] = {**scope_doc, "scope": scope, "contract_pure": contract_pure}
    treasury_review = treasury_futures_review(text, scope)
    result["treasury_futures_review"] = {**treasury_review, "source": latest_doc["source"]}
    treasury_clear = treasury_review["status"] in ("clear", "not_applicable")
    if treasury_review["status"] == "non_pure":
        result["pure_bond"] = False
    elif not treasury_clear:
        issues.append({"kind": "treasury_futures_unverified", "message": "国债期货用途、品种和敞口披露尚未核实；交易事实不是非纯债剔除依据", "checks": treasury_review["checks"], "source": latest_doc["source"]})
    if not scope:
        issues.append({"kind": "scope_unparsed", "message": "招募说明书已取得，但投资范围未识别", "source": scope_doc["source"]})
    later = [entry for entry in available.values() if entry["PUBLISHDATEDesc"] > latest_doc["publication_date"] and re.search(r"投资范围|基金合同|转型|合并|终止|清算|收益异常|净值更正", entry["TITLE"]) and entry["ID"] != prospectus["ID"]]
    result["later_relevant_announcements"] = later
    if later:
        announcement_reviews = review_later_announcements(client, later, code, cache, full_name, today, contract_pure)
        result["later_announcement_reviews"] = announcement_reviews
        unresolved = {record["announcement_id"] for record in announcement_reviews if record["status"] == "unverified"}
        later = [entry for entry in later if entry["ID"] in unresolved]
        if later:
            issues.append({"kind": "later_announcement_unverified", "message": "定期报告后存在尚未完成原件核查的相关公告", "sources": [entry["ID"] for entry in later]})
    if holdings["status"] == "clear" and scope and not later and treasury_clear:
        if contract_pure is True:
            result["pure_bond"] = True
        else:
            interim_reports = [entry for entry in reports if re.search(r"中期报告|半年度报告|年度报告", entry["TITLE"])]
            if interim_reports:
                interim_entry = max(interim_reports, key=lambda entry: (period_end(entry["TITLE"]), entry["PUBLISHDATEDesc"]))
                interim_text, interim_doc = document(client, interim_entry, code, cache, refresh=interim_entry["ID"] != latest["ID"])
                if compact(full_name) not in compact(interim_text):
                    raise ValueError("交易报告身份不一致")
                interim_holdings = portfolio(interim_text)
                interim_treasury = treasury_futures_review(interim_text, scope)
                result["interim_treasury_futures_review"] = {**interim_treasury, "source": interim_doc["source"]}
                components = {"interim_holdings": interim_holdings["status"] == "clear", "stock_transactions": stock_trades_clear(interim_text), "permitted_non_stock_transactions": permitted_non_stock_trades_clear(interim_text, scope), "treasury_futures": interim_treasury["status"] in ("clear", "not_applicable"), "latest_other_assets": other_assets_clear(text), "interim_other_assets": other_assets_clear(interim_text)}
                trades = non_pure_period_trades(interim_text)
                components["no_confirmed_non_pure_transactions"] = not trades
                clear = all(value for key, value in components.items() if key != "permitted_non_stock_transactions")
                result["pure_bond"] = False if interim_holdings["status"] == "non_pure" or interim_treasury["status"] == "non_pure" or trades else True if clear else None
                result["pure_bond_basis"] = "actual_holdings"
                result["pure_bond_review"] = {"report_date": latest_doc["report_date"], "checked_on": today, "coverage_complete": clear, "non_pure_assets_held": True if trades else False if clear else None, "sources": [latest_doc["source"], scope_doc["source"], interim_doc["source"]], "findings": ["完整资产组合、债券品种、其他资产和股票交易记录已核查；期内非纯债交易仅按明确事实判定，不把缺少全期否定声明当作已投资。", f"合同许可范围：{scope}；仅代表最新披露，持仓有滞后。"]}
                result["pure_bond_review"]["component_checks"] = components
                if trades:
                    result["pure_bond_review"]["confirmed_non_pure_transactions"] = trades
                if clear and not components["permitted_non_stock_transactions"]:
                    result["disclosure_notes"].append({"kind": "period_non_pure_absence_not_proven", "message": "备注保留：完整披露持仓及股票交易核查通过，未检出明确非纯债交易；合同仍允许相关品种，缺少全期未投资的逐品种声明，不保证报告日后或未来配置。", "sources": [latest_doc["source"], scope_doc["source"], interim_doc["source"]]})
                if not clear:
                    issues.append({"kind": "purity_components_incomplete", "message": "实际持仓例外尚未完成全部核查", "components": [key for key, value in components.items() if not value], "source": interim_doc["source"]})
            else:
                issues.append({"kind": "source_unavailable", "message": "实际持仓例外所需的中期或年度交易报告未取得"})
    reviewed_years = []
    sources = []
    clues = []
    confirmed = []
    quote_checks = []
    for year in range(earliest, int(as_of[:4])):
        annual_entries = [entry for entry in reports if period_end(entry["TITLE"]) == f"{year}-12-31" and re.search(r"年度报告|年报", entry["TITLE"])]
        if not annual_entries:
            quote_checks.append({"year": year, "status": "missing_official_annual_report"})
            issues.append({"kind": "source_unavailable", "year": year, "message": "官方完整年报未取得"})
            continue
        entry = max(annual_entries, key=lambda item: (item["PUBLISHDATEDesc"], item["ID"]))
        try:
            annual_text, annual_doc = document(client, entry, code, cache)
        except (RuntimeError, ValueError, PyPdfError, UnicodeError, OSError) as error:
            kind = "source_unavailable" if "数据请求失败" in str(error) else "document_parse_failed"
            issues.append({"kind": kind, "year": year, "message": str(error), "source": f"https://fund.eastmoney.com/gonggao/{code},{entry['ID']}.html"})
            quote_checks.append({"year": year, "status": kind})
            continue
        identity = historical_identity(annual_text, full_name, scope_text, declared_codes)
        result.setdefault("historical_identity_checks", []).append({"year": year, **identity, "sources": [scope_doc["source"], annual_doc["source"]]})
        if not identity["verified"]:
            issues.append({"kind": "document_identity_mismatch", "year": year, "message": "历史年度报告身份不一致", "source": annual_doc["source"]})
            continue
        sources.append(annual_doc["source"])
        events = event_findings(annual_text, year, annual_doc["source"])
        clues.extend(events)
        if events:
            issues.append({"kind": "annual_event_unverified", "year": year, "message": "报告包含实际非经常性事件线索，需核实影响", "source": annual_doc["source"]})
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
            quote = yearly_quote(annual_text, fund["name"], len(active_classes) == 1 and fund["code"] in annual_codes, year=year)
            matched = quote is not None and abs(quote - Decimal(expected)) <= Decimal("0.02") and (quote < 0) == (Decimal(expected) < 0) and (quote > 0) == (Decimal(expected) > 0)
            disclosed = not matched and quote is not None and quote >= 0 and Decimal(expected) >= 0
            checks.append({"code": fund["code"], "expected": expected, "official_quote": str(quote) if quote is not None else None, "matched": matched, "conflict_disclosed": disclosed})
            if disclosed:
                result["annual_return_conflicts"].setdefault(fund["code"], {})[str(year)] = [str(expected), str(quote)]
                result["disclosure_notes"].append({"code": fund["code"], "kind": "annual_quote_conflict", "year": year, "platform_pct": str(expected), "official_pct": str(quote), "message": f"年度收益来源有差异：{year}年平台{expected}%、年报{quote}%；保留双方数值，须验证差异不改变筛选资格，不认定为异常事件。", "sources": [annual_doc["source"]]})
            elif not matched:
                issues.append({"kind": "annual_quote_unparsed" if quote is None else "annual_quote_mismatch", "year": year, "code": fund["code"], "message": "年报已取得但未识别出对应份额的全年收益" if quote is None else f"平台收益{expected}%与年报收益{quote}%未通过一致性核验", "source": annual_doc["source"]})
        quote_checks.append({"year": year, "checks": checks, "source": annual_doc["source"]})
        if checks and all(check["matched"] or check["conflict_disclosed"] for check in checks) and not events:
            reviewed_years.append(year)
    result["annual_quote_checks"] = quote_checks
    result["annual_event_clues"] = clues
    complete = reviewed_years == list(range(earliest, int(as_of[:4]))) and not result["unverified_share_codes"]
    result["annual_anomaly_review"] = {"status": "confirmed" if confirmed else "clear" if complete and not clues else "unverified", "years": reviewed_years, "sources": sources, "findings": confirmed}
    return result
