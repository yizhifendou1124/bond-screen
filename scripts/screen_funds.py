import argparse
import copy
import json
import re
import sys
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path


QUALIFIED_GROUP_ORDER = (
    "BOTH_HORIZONS_AT_LEAST_TARGET",
    "ONE_HORIZON_AT_LEAST_TARGET",
    "BOTH_LOWER_RECENT_EXCEPTION",
)


def percentage(value, label):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{label}: 收益率不能是布尔值")
    try:
        number = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError(f"{label}: 收益率必须为百分数数值，不带百分号") from error
    if not number.is_finite():
        raise ValueError(f"{label}: 收益率必须为有限数值")
    return number


def annualized(fund, years):
    if not comparable_history(fund):
        return None
    values = [fund["annual_returns"].get(year) for year in years]
    if any(value is None for value in values):
        return None
    with localcontext() as context:
        context.prec = 40
        product = Decimal(1)
        for value in values:
            product *= Decimal(1) + value / Decimal(100)
        return (product ** (Decimal(1) / Decimal(len(values))) - 1) * 100


def displayed(value):
    return None if value is None else str(value.quantize(Decimal("0.01")))


def qualified_sort_key(fund):
    ytd = percentage(fund["ytd"], f"{fund['code']}/sort_ytd")
    if ytd is None:
        raise ValueError("正式入选基金排序必须提供原始今年收益")
    return QUALIFIED_GROUP_ORDER.index(fund["reason"]), ytd.copy_negate(), fund["code"]


def assets_amount(value, label):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{label}: 规模不能是布尔值")
    try:
        number = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError(f"{label}: 规模必须为人民币元数值") from error
    if not number.is_finite() or number < 0:
        raise ValueError(f"{label}: 规模必须为非负有限数值")
    return number


def assets_eligibility(fund, minimum):
    missing = []
    if fund["fund_assets_cny"] is None:
        missing.append("FUND_ASSETS_MISSING")
    if fund["fund_assets_basis"] != "fund_total":
        missing.append("FUND_ASSETS_TOTAL_UNCONFIRMED")
    if fund["fund_assets_date"] is None:
        missing.append("FUND_ASSETS_DATE_MISSING")
    if not fund["fund_assets_sources"]:
        missing.append("FUND_ASSETS_SOURCE_MISSING")
    return (None if missing else fund["fund_assets_cny"] >= minimum), missing


def subscription_eligibility(fund, screen_date):
    missing = []
    if fund["subscription_status"] in (None, "unverified"):
        missing.append("SUBSCRIPTION_STATUS_UNCONFIRMED")
    if fund["subscription_checked_on"] != screen_date:
        missing.append("SUBSCRIPTION_CURRENT_DATE_UNCONFIRMED")
    if not fund["subscription_sources"]:
        missing.append("SUBSCRIPTION_SOURCE_MISSING")
    if not fund["subscription_channel"]:
        missing.append("SUBSCRIPTION_CHANNEL_MISSING")
    if missing:
        return None, missing
    if fund["subscription_status"] == "closed" or fund["subscription_limit_cny"] == 0:
        return False, []
    if fund["subscription_status"] == "limited" and fund["subscription_limit_cny"] is None:
        return None, ["SUBSCRIPTION_POSITIVE_LIMIT_UNCONFIRMED"]
    return True, []


def complete_history(fund, years):
    return comparable_history(fund) and all(
        fund["annual_returns"].get(year) is not None for year in years
    )


def comparable_history(fund):
    return fund["history_basis"] in ("share_class", "same_fund_completed")


def nonpositive_annual_returns(fund, years):
    if not comparable_history(fund):
        return {}
    return {
        str(year): str(fund["annual_returns"][year])
        for year in years
        if fund["annual_returns"].get(year) is not None and fund["annual_returns"][year] <= 0
    }


def normalize_anomaly_review(raw, as_of, code):
    if raw is None:
        return {"status": "unverified", "years": [], "sources": [], "findings": []}
    if not isinstance(raw, dict):
        raise ValueError(f"{code}: annual_anomaly_review必须为对象或null")
    status = raw.get("status")
    if status not in ("clear", "confirmed", "unverified"):
        raise ValueError(f"{code}: 异常年份核查状态必须为clear、confirmed或unverified")
    years = raw.get("years", [])
    cutoff_year = date.fromisoformat(as_of).year
    if (
        not isinstance(years, list)
        or any(type(year) is not int or not 1900 <= year < cutoff_year for year in years)
        or len(set(years)) != len(years)
    ):
        raise ValueError(f"{code}: 异常核查年份必须为无重复的完整已结束年份列表")
    sources = raw.get("sources", [])
    if not isinstance(sources, list) or any(
        not isinstance(source, str) or not source.strip() for source in sources
    ) or (status != "unverified" and not sources):
        raise ValueError(f"{code}: 已完成异常核查必须有非空来源链接列表")
    findings = raw.get("findings", [])
    if not isinstance(findings, list) or (status == "confirmed" and not findings) or (status == "clear" and findings):
        raise ValueError(f"{code}: confirmed必须有异常记录，clear不能有异常记录")
    normalized = []
    for finding in findings:
        if not isinstance(finding, dict):
            raise ValueError(f"{code}: 异常记录必须为对象")
        year = finding.get("year")
        reason = finding.get("reason")
        evidence = finding.get("sources", [])
        if type(year) is not int or not 1900 <= year <= cutoff_year:
            raise ValueError(f"{code}: 异常事件年份不能晚于截止年")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"{code}: 异常记录必须有核查原因")
        if not isinstance(evidence, list) or not evidence or any(
            not isinstance(source, str) or not source.strip() for source in evidence
        ):
            raise ValueError(f"{code}: 异常记录必须有匹配事件的来源链接")
        value = percentage(finding.get("return_pct"), f"{code}/{year}/异常核查收益")
        normalized.append({
            "year": year, "return_pct": None if value is None else str(value),
            "reason": reason, "sources": list(evidence),
        })
    return {"status": status, "years": sorted(years), "sources": list(sources), "findings": normalized}


def quantitative_annual_anomalies(fund, threshold, earliest_year=2020):
    annual = {year: value for year, value in fund["annual_returns"].items() if value is not None and year >= earliest_year}
    if not comparable_history(fund) or len(annual) < 2:
        return {"status": "unverified", "years": sorted(annual), "findings": []}
    findings = []
    with localcontext() as context:
        context.prec = 40
        total = sum(annual.values(), Decimal(0))
        other_count = Decimal(len(annual) - 1)
        for year, value in sorted(annual.items()):
            other_total = total - value
            if value * other_count - other_total >= threshold * other_count:
                mean = other_total / other_count
                findings.append({
                    "year": year, "return_pct": str(value),
                    "mean_other_years_pct": str(mean), "gap_pp": str(value - mean),
                    "baseline_years": sorted(item for item in annual if item != year),
                    "rule": "ANNUAL_RETURN_ABOVE_OTHER_YEARS_MEAN",
                    "reason": "全年收益高于其他完整年度算术平均收益至少门槛个百分点",
                    "sources": list(fund["sources"]),
                    "data_completion": dict(fund["data_completion"]),
                })
    return {"status": "confirmed" if findings else "clear", "years": sorted(annual), "findings": findings}


def review_annual_anomalies(funds, groups, required_years, quantitative_reviews, earliest_year=2020):
    cohorts = {code: [code] for code in funds}
    for group in groups:
        members = [code for code in group["codes"] if code in funds]
        for code in members:
            cohorts[code] = list(group["codes"])
    reviews = {}
    for code, cohort in cohorts.items():
        members = [member for member in cohort if member in funds]
        years = set(required_years)
        for member in members:
            years.update(year for year, value in funds[member]["annual_returns"].items() if value is not None and year >= earliest_year)
        findings = []
        unverified = [member for member in cohort if member not in funds]
        confirmed = []
        for member in members:
            review = funds[member]["annual_anomaly_review"]
            relevant_findings = [finding for finding in review["findings"] if finding["year"] >= earliest_year]
            findings.extend({**finding, "source_code": member} for finding in relevant_findings)
            quantitative = quantitative_reviews[member]
            findings.extend({**finding, "source_code": member} for finding in quantitative["findings"])
            if (review["status"] == "confirmed" and relevant_findings) or quantitative["status"] == "confirmed":
                confirmed.append(member)
            elif review["status"] == "unverified" or quantitative["status"] != "clear" or not years.issubset(review["years"]):
                unverified.append(member)
        reviews[code] = {
            "status": "confirmed" if confirmed else "unverified" if unverified else "clear",
            "required_years": sorted(years), "same_fund_codes": list(cohort),
            "confirmed_codes": confirmed, "unverified_codes": unverified,
            "findings": findings,
        }
    return reviews


def normalize_purity_review(raw, as_of, code):
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"{code}: pure_bond_review必须为对象或null")
    normalized = {}
    for field in ["report_date", "checked_on"]:
        value = raw.get(field)
        if value is not None:
            if not isinstance(value, str):
                raise ValueError(f"{code}: {field}必须为ISO日期字符串或null")
            parsed = date.fromisoformat(value)
            if field == "report_date" and parsed > date.fromisoformat(as_of):
                raise ValueError(f"{code}: 持仓报告日不能晚于净值截止日")
        normalized[field] = value
    if normalized["report_date"] and normalized["checked_on"] and normalized["checked_on"] < normalized["report_date"]:
        raise ValueError(f"{code}: 持仓核查日不能早于报告日")
    for field in ["sources", "findings"]:
        values = raw.get(field, [])
        if not isinstance(values, list) or any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError(f"{code}: {field}必须为非空字符串组成的列表")
        normalized[field] = list(values)
    for field in ["coverage_complete", "non_pure_assets_held"]:
        value = raw.get(field)
        if value is not None and not isinstance(value, bool):
            raise ValueError(f"{code}: {field}必须为bool或null")
        normalized[field] = value
    return normalized


def purity_eligibility(fund, screen_date):
    review = fund["pure_bond_review"]
    if fund["pure_bond"] is False or (review and review["non_pure_assets_held"] is True):
        return False, []
    missing = [] if fund["pure_bond"] is True else ["PURE_BOND_UNCONFIRMED"]
    if fund["pure_bond_basis"] == "actual_holdings":
        if review is None:
            missing.append("PURE_BOND_HOLDINGS_REVIEW_MISSING")
        else:
            for field in ["report_date", "sources", "findings"]:
                if not review[field]:
                    missing.append(f"PURE_BOND_{field.upper()}_MISSING")
            if review["checked_on"] != screen_date:
                missing.append("PURE_BOND_CURRENT_REVIEW_UNCONFIRMED")
            if review["coverage_complete"] is not True or review["non_pure_assets_held"] is not False:
                missing.append("PURE_BOND_COMPLETE_HOLDINGS_UNCONFIRMED")
    return (None if missing else True), missing


def normalize_fund(raw, as_of):
    code = raw["code"]
    if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code):
        raise ValueError("基金代码必须为保留前导零的六位字符串")
    if raw["data_date"] != as_of:
        raise ValueError(f"{code}: data_date必须与as_of一致")
    purity = raw["pure_bond"]
    if purity is not None and not isinstance(purity, bool):
        raise ValueError(f"{code}: pure_bond必须为true、false或null")
    purity_basis = raw.get("pure_bond_basis", "contract")
    if purity_basis not in ("contract", "actual_holdings"):
        raise ValueError(f"{code}: pure_bond_basis必须为contract或actual_holdings")
    purity_review = normalize_purity_review(raw.get("pure_bond_review"), as_of, code)
    basis = raw["history_basis"]
    if basis not in ("share_class", "proxy"):
        raise ValueError(f"{code}: history_basis必须为share_class或proxy")
    assets = assets_amount(raw.get("fund_assets_cny"), f"{code}/fund_assets_cny")
    assets_basis = raw.get("fund_assets_basis")
    if assets_basis not in (None, "fund_total", "share_class"):
        raise ValueError(f"{code}: fund_assets_basis必须为fund_total、share_class或null")
    assets_date = raw.get("fund_assets_date")
    if assets_date is not None:
        if not isinstance(assets_date, str):
            raise ValueError(f"{code}: 规模报告日必须为ISO日期字符串或null")
        if date.fromisoformat(assets_date) > date.fromisoformat(as_of):
            raise ValueError(f"{code}: 规模报告日不能晚于截止日")
    assets_sources = raw.get("fund_assets_sources", [])
    if not isinstance(assets_sources, list) or any(
        not isinstance(source, str) or not source.strip() for source in assets_sources
    ):
        raise ValueError(f"{code}: fund_assets_sources必须为非空字符串组成的列表")
    subscription_status = raw.get("subscription_status")
    if subscription_status not in (None, "open", "limited", "closed", "unverified"):
        raise ValueError(f"{code}: subscription_status必须为open、limited、closed、unverified或null")
    subscription_date = raw.get("subscription_checked_on")
    if subscription_date is not None:
        if not isinstance(subscription_date, str):
            raise ValueError(f"{code}: 申购核查日必须为ISO日期字符串或null")
        date.fromisoformat(subscription_date)
    subscription_sources = raw.get("subscription_sources", [])
    if not isinstance(subscription_sources, list) or any(
        not isinstance(source, str) or not source.strip() for source in subscription_sources
    ):
        raise ValueError(f"{code}: subscription_sources必须为非空字符串组成的列表")
    subscription_channel = raw.get("subscription_channel")
    if subscription_channel is not None and (not isinstance(subscription_channel, str) or not subscription_channel.strip()):
        raise ValueError(f"{code}: subscription_channel必须为非空渠道字符串或null")
    subscription_limit = assets_amount(raw.get("subscription_limit_cny"), f"{code}/subscription_limit_cny")
    inception = None
    if raw.get("inception_date"):
        inception = date.fromisoformat(raw["inception_date"])
        if inception > date.fromisoformat(as_of):
            raise ValueError(f"{code}: 成立时间不能晚于截止日")
    annual_returns = {}
    for year_text, value in raw["annual_returns"].items():
        if not re.fullmatch(r"[0-9]{4}", str(year_text)):
            raise ValueError(f"{code}: 年度键必须为四位年份")
        year = int(year_text)
        number = percentage(value, f"{code}/{year}")
        if number is not None:
            if year >= date.fromisoformat(as_of).year:
                raise ValueError(f"{code}/{year}: 尚非完整已结束年度")
            if number < Decimal(-100):
                raise ValueError(f"{code}/{year}: 全年收益不能低于-100%")
            if basis == "share_class" and inception and inception > date(year, 1, 1):
                raise ValueError(f"{code}/{year}: 该份额尚未经历完整年度")
        annual_returns[year] = number
    return {
        "code": code,
        "name": raw["name"],
        "pure_bond": purity,
        "pure_bond_basis": purity_basis,
        "pure_bond_review": purity_review,
        "disclosure_notes": copy.deepcopy(raw.get("disclosure_notes", [])),
        "history_basis": basis,
        "annual_returns": annual_returns,
        "ytd": percentage(raw["ytd"], f"{code}/ytd"),
        "recent_6m": percentage(raw.get("recent_6m"), f"{code}/recent_6m"),
        "recent_3m": percentage(raw.get("recent_3m"), f"{code}/recent_3m"),
        "sources": raw.get("sources", []),
        "data_completion": {},
        "annual_anomaly_review": normalize_anomaly_review(raw.get("annual_anomaly_review"), as_of, code),
        "fund_assets_cny": assets,
        "fund_assets_basis": assets_basis,
        "fund_assets_date": assets_date,
        "fund_assets_sources": assets_sources,
        "subscription_status": subscription_status,
        "subscription_checked_on": subscription_date,
        "subscription_sources": subscription_sources,
        "subscription_channel": subscription_channel,
        "subscription_limit_cny": subscription_limit,
    }


def code_list(values, label):
    if (
        not isinstance(values, list)
        or any(not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code) for code in values)
        or len(set(values)) != len(values)
    ):
        raise ValueError(f"{label}必须为无重复的六位代码字符串列表")
    return list(values)


def complete_same_fund_data(funds, groups, enabled, required_years):
    if not isinstance(enabled, bool):
        raise ValueError("history_fill_enabled必须为布尔值")
    if not isinstance(groups, list):
        raise ValueError("history_fill_groups必须为列表")
    covered = set()
    original = {
        code: {**fund, "annual_returns": dict(fund["annual_returns"])}
        for code, fund in funds.items()
    }
    for group in groups:
        codes = code_list(group["codes"], "history_fill_groups/codes")
        preferred = code_list(group.get("preferred_codes", []), "history_fill_groups/preferred_codes")
        identity_sources = group.get("sources", [])
        if len(codes) < 2 or covered.intersection(codes):
            raise ValueError("同基金补全组至少两个份额，组间不得重叠")
        if not set(preferred).issubset(codes):
            raise ValueError("优先补全来源必须属于同基金补全组")
        if not isinstance(identity_sources, list) or not identity_sources or any(
            not isinstance(source, str) or not source.strip() for source in identity_sources
        ):
            raise ValueError("同基金补全组必须提供身份核验来源")
        covered.update(codes)
        if not enabled:
            continue
        donors = [
            original[code] for code in codes
            if code in original and original[code]["history_basis"] == "share_class"
        ]
        donors.sort(key=lambda fund: (
            preferred.index(fund["code"]) if fund["code"] in preferred else len(preferred),
            -sum(value is not None for year, value in fund["annual_returns"].items() if year in required_years),
            fund["code"],
        ))
        years = sorted(set(required_years))
        for code in codes:
            if code not in funds or original[code]["history_basis"] != "share_class":
                continue
            recipient = funds[code]
            for field in ["annual_returns", "ytd", "recent_6m", "recent_3m"]:
                keys = years if field == "annual_returns" else [None]
                for year in keys:
                    current = recipient[field].get(year) if year is not None else recipient[field]
                    if current is not None:
                        continue
                    donor = next((
                        source for source in donors
                        if source["code"] != code and (
                            source[field].get(year) if year is not None else source[field]
                        ) is not None
                    ), None)
                    if donor is None:
                        continue
                    if not isinstance(donor["sources"], list) or not donor["sources"] or any(
                        not isinstance(source, str) or not source.strip() for source in donor["sources"]
                    ):
                        raise ValueError(f"{donor['code']}: 补全来源缺少收益核查链接")
                    value = donor[field][year] if year is not None else donor[field]
                    provenance = {
                        "source_code": donor["code"], "source_name": donor["name"],
                        "return_pct": str(value), "sources": donor["sources"],
                        "identity_sources": identity_sources,
                    }
                    if year is not None:
                        recipient[field][year] = value
                        recipient["data_completion"].setdefault(field, {})[str(year)] = provenance
                        recipient["history_basis"] = "same_fund_completed"
                    else:
                        recipient[field] = value
                        recipient["data_completion"][field] = provenance


def return_conflict_bounds(candidate, raw, earliest_year):
    bounds = {year: (value, value) for year, value in candidate["annual_returns"].items() if value is not None and year >= earliest_year}
    updates = {}
    for year in bounds:
        source_code = candidate["data_completion"].get("annual_returns", {}).get(str(year), {}).get("source_code", candidate["code"])
        conflicts = raw[source_code].get("annual_return_conflicts", {})
        if str(year) not in conflicts:
            continue
        values = [percentage(value, f"{source_code}/{year}/conflict") for value in conflicts[str(year)]]
        if len(values) < 2 or any(value is None or value < -100 for value in values) or candidate["annual_returns"][year] not in values:
            raise ValueError("收益冲突必须包含实际收益及至少两个有效来源值")
        bounds[year] = (min(values), max(values))
        updates.setdefault(source_code, {})[str(year)] = bounds[year]
    return bounds, updates


def check_return_disclosures(data, funds, results, earliest_year, anomaly_gap, target_override, mode_override, history_fill_groups):
    raw = {fund["code"]: fund for fund in data["funds"]}
    for result in results:
        if result["status"] != "qualified":
            continue
        bounds, updates = return_conflict_bounds(funds[result["code"]], raw, earliest_year)
        family_bounds = [bounds]
        for group in history_fill_groups:
            if result["code"] not in group["codes"]:
                continue
            for code in group["codes"]:
                if code not in funds or code == result["code"]:
                    continue
                member_bounds, member_updates = return_conflict_bounds(funds[code], raw, earliest_year)
                family_bounds.append(member_bounds)
                for source_code, years in member_updates.items():
                    updates.setdefault(source_code, {}).update(years)
        if not updates:
            continue
        sensitive = any(lower <= 0 for lower, upper in bounds.values())
        for member_bounds in family_bounds:
            for year, (lower, upper) in member_bounds.items():
                other_lower = [other[0] for other_year, other in member_bounds.items() if other_year != year]
                if not other_lower or upper * len(other_lower) - sum(other_lower) >= anomaly_gap * len(other_lower):
                    sensitive = True
        probes = []
        if not sensitive:
            for index in (0, 1):
                probe_data = copy.deepcopy(data)
                for fund in probe_data["funds"]:
                    for year, values in updates.get(fund["code"], {}).items():
                        fund["annual_returns"][year] = str(values[index])
                probe = screen(probe_data, target_override, mode_override, check_conflicts=False)
                probes.append(next(item for item in probe["results"] if item["code"] == result["code"]))
            sensitive = any(probe["status"] != "qualified" for probe in probes)
        result["return_source_sensitivity"] = {"decision_stable": not sensitive, "method": "candidate_lower_upper_bounds_and_worst_case_annual_anomaly", "bounds_pct": {str(year): [str(value) for value in values] for year, values in bounds.items()}, "probe_reasons": [probe["reason"] for probe in probes]}
        result["return_source_sensitivity"]["classification_stable"] = len({probe["reason"] for probe in probes}) == 1 if not sensitive else None
        if not sensitive and not result["return_source_sensitivity"]["classification_stable"]:
            result["disclosure_notes"].append({"kind": "source_conflict_classification", "message": "来源差异不改变入选资格，但可能改变三五年分类；当前按展示的平台收益分类。"})
        for source_code, years in updates.items():
            if source_code == result["code"]:
                for year, values in years.items():
                    if not any(note.get("kind") == "annual_quote_conflict" and str(note.get("year")) == year for note in result["disclosure_notes"]):
                        result["disclosure_notes"].append({"kind": "annual_quote_conflict", "year": year, "message": f"{year}年收益来源区间为{values[0]}%至{values[1]}%，保留原值及来源差异，不认定为异常事件。"})
            else:
                inherited = any(source.get("source_code") == source_code for source in funds[result["code"]]["data_completion"].get("annual_returns", {}).values())
                result["disclosure_notes"].append({"kind": "inherited_annual_quote_conflict" if inherited else "same_fund_annual_quote_conflict", "source_code": source_code, "years": sorted(years), "message": "同基金其他份额或补全来源存在年度收益差异，整基金异常限制及补全值均已纳入校核，不隐去来源疑点。"})
        if sensitive:
            result.update(status="observation", reason="RETURN_SOURCE_CONFLICT_DECISION_SENSITIVE", missing=["RETURN_SOURCE_CONFLICT_DECISION_SENSITIVE"], retained_with_notes=False)
        else:
            result["retained_with_notes"] = True


def screen(data, target_override=None, mode_override=None, check_conflicts=True):
    defaults_path = Path(__file__).resolve().parent.parent / "references" / "defaults.json"
    with defaults_path.open(encoding="utf-8") as handle:
        defaults = json.load(handle)
    as_of = data["as_of"]
    cutoff = date.fromisoformat(as_of)
    screen_date = data.get("screen_date", date.today().isoformat())
    if date.fromisoformat(screen_date) < cutoff:
        raise ValueError("screen_date不能早于净值截止日")
    mode = mode_override if mode_override is not None else data.get("mode", defaults["mode"])
    if mode not in ("table", "market"):
        raise ValueError("mode必须为table或market")
    minimum_assets = assets_amount(data.get("min_fund_assets_cny", defaults["min_fund_assets_cny"]), "min_fund_assets_cny")
    if minimum_assets is None or minimum_assets <= 0:
        raise ValueError("min_fund_assets_cny必须为正数人民币元金额")
    anomaly_gap = percentage(data.get("annual_anomaly_gap_pp", defaults["annual_anomaly_gap_pp"]), "annual_anomaly_gap_pp")
    if anomaly_gap is None or anomaly_gap <= 0:
        raise ValueError("annual_anomaly_gap_pp必须为正数百分点")
    target_ytd_gap = percentage(data.get("target_ytd_gap_pp", defaults["target_ytd_gap_pp"]), "target_ytd_gap_pp")
    if target_ytd_gap is None or target_ytd_gap <= 0:
        raise ValueError("target_ytd_gap_pp必须为正数百分点")
    earliest_year = data.get("earliest_screen_year", defaults["earliest_screen_year"])
    if type(earliest_year) is not int or not 1900 <= earliest_year < cutoff.year:
        raise ValueError("earliest_screen_year必须为已结束的整数年份")
    end_year = data["calendar_end_year"]
    if type(end_year) is not int or not 1905 <= end_year < cutoff.year:
        raise ValueError("calendar_end_year必须是截止日前已完整结束的年度")
    hard_years = data.get("hard_years", defaults["hard_years"])
    if (
        not isinstance(hard_years, list)
        or not hard_years
        or any(type(year) is not int or not earliest_year <= year < cutoff.year for year in hard_years)
        or len(set(hard_years)) != len(hard_years)
    ):
        raise ValueError("hard_years必须为非空、不重复、已结束年份列表")
    hard_years = sorted(hard_years)
    three_years = list(range(end_year - 2, end_year + 1))
    five_years = list(range(end_year - 4, end_year + 1))
    if five_years[0] < earliest_year:
        raise ValueError("完整五年窗口不能早于earliest_screen_year，不能缩短窗口冒充五年年化")
    funds = {}
    for raw in data["funds"]:
        fund = normalize_fund(raw, as_of)
        if fund["code"] in funds:
            raise ValueError(f"重复基金代码: {fund['code']}")
        funds[fund["code"]] = fund
    history_fill_enabled = data.get("history_fill_enabled", defaults.get("history_fill_enabled", True))
    history_fill_groups = data.get("history_fill_groups", defaults.get("history_fill_groups", []))
    acquired_years = sorted({year for fund in funds.values() for year in fund["annual_returns"] if year >= earliest_year})
    positive_years = list(range(earliest_year, cutoff.year))
    complete_same_fund_data(funds, history_fill_groups, history_fill_enabled, hard_years + three_years + five_years + acquired_years + positive_years)
    quantitative_reviews = {code: quantitative_annual_anomalies(fund, anomaly_gap, earliest_year) for code, fund in funds.items()}
    anomaly_reviews = review_annual_anomalies(funds, history_fill_groups, hard_years + three_years + five_years, quantitative_reviews, earliest_year)
    reference_codes = code_list(data.get("reference_codes", defaults.get("reference_codes", [])), "reference_codes")
    exclude_codes = code_list(data.get("exclude_codes", defaults.get("exclude_codes", [])), "exclude_codes")
    target_code = target_override if target_override is not None else data.get("target_code", defaults["target_code"])
    code_list([target_code], "target_code")
    target_source = "override" if target_override is not None else "input" if "target_code" in data else "preset"
    if target_code not in funds:
        raise ValueError(f"指定或预制目标{target_code}的数据未提供，不能回退到其他目标")
    if target_code not in reference_codes:
        reference_codes.append(target_code)
    if any(code not in funds for code in reference_codes):
        raise ValueError("必须提供全部参考基金数据，缺失收益应填null而非删除参考")
    target = funds[target_code]
    if purity_eligibility(target, screen_date)[0] is not True:
        raise ValueError("目标必须先确认纯债属性")
    target_three = annualized(target, three_years)
    target_five = annualized(target, five_years)
    target_missing = []
    raw = {fund["code"]: fund for fund in data["funds"]}
    if return_conflict_bounds(target, raw, earliest_year)[1]:
        target_missing.append("TARGET_RETURN_SOURCE_CONFLICT_UNCONFIRMED")
    if anomaly_reviews[target_code]["status"] != "clear":
        target_missing.append(
            "TARGET_ANNUAL_ANOMALY_CONFIRMED" if anomaly_reviews[target_code]["status"] == "confirmed"
            else "TARGET_ANNUAL_ANOMALY_REVIEW_UNCONFIRMED"
        )
    if not complete_history(target, hard_years):
        target_missing.append("TARGET_HARD_YEAR_HISTORY_INCOMPLETE")
    if target_three is None or target_five is None:
        target_missing.append("TARGET_LONG_TERM_HISTORY_INCOMPLETE")
    if target["ytd"] is None:
        target_missing.append("TARGET_YTD_MISSING")
    reference_missing = [code for code in reference_codes if not complete_history(funds[code], hard_years)]
    reference_ytd_missing = [code for code in reference_codes if funds[code]["ytd"] is None]
    reference_anomaly_missing = [code for code in reference_codes if anomaly_reviews[code]["status"] != "clear"]
    reference_conflict_missing = [code for code in reference_codes if return_conflict_bounds(funds[code], raw, earliest_year)[1]]
    floor = target["ytd"] - Decimal("0.30") if target["ytd"] is not None else None
    results = []
    for code, candidate in funds.items():
        three = annualized(candidate, three_years)
        five = annualized(candidate, five_years)
        assets_eligible, assets_missing = assets_eligibility(candidate, minimum_assets)
        subscription_eligible, subscription_missing = subscription_eligibility(candidate, screen_date)
        purity_eligible, purity_missing = purity_eligibility(candidate, screen_date)
        advantages = {
            str(year): str(candidate["annual_returns"][year] - target["annual_returns"][year])
            for year in hard_years
            if comparable_history(candidate) and comparable_history(target) and candidate["annual_returns"].get(year) is not None and target["annual_returns"].get(year) is not None
        }
        result = {
            "code": code, "name": candidate["name"],
            "cagr3": displayed(three), "cagr5": displayed(five),
            "ytd": displayed(candidate["ytd"]),
            "recent_6m": displayed(candidate["recent_6m"]),
            "recent_3m": displayed(candidate["recent_3m"]),
            "ytd_gap_vs_target_pp": displayed(candidate["ytd"] - target["ytd"]) if candidate["ytd"] is not None and target["ytd"] is not None else None,
            "annual_advantages_vs_target_pp": advantages,
            "witnesses": [], "ranking_only_dominators": [],
            "sources": candidate["sources"],
            "pure_bond_basis": candidate["pure_bond_basis"],
            "pure_bond_review": candidate["pure_bond_review"],
            "disclosure_notes": candidate["disclosure_notes"],
            "retained_with_notes": False,
            "pure_bond_eligible": purity_eligible,
            "pure_bond_missing": purity_missing,
            "history_basis": candidate["history_basis"],
            "data_completion": candidate["data_completion"],
            "annual_anomaly_review": candidate["annual_anomaly_review"],
            "annual_anomaly_eligibility": anomaly_reviews[code],
            "quantitative_annual_anomaly_test": quantitative_reviews[code],
            "nonpositive_annual_returns_pct": nonpositive_annual_returns(candidate, positive_years),
            "fund_assets_cny": None if candidate["fund_assets_cny"] is None else str(candidate["fund_assets_cny"]),
            "fund_assets_basis": candidate["fund_assets_basis"],
            "fund_assets_date": candidate["fund_assets_date"],
            "fund_assets_sources": candidate["fund_assets_sources"],
            "fund_assets_eligible": assets_eligible,
            "fund_assets_missing": assets_missing,
            "subscription_status": candidate["subscription_status"],
            "subscription_checked_on": candidate["subscription_checked_on"],
            "subscription_sources": candidate["subscription_sources"],
            "subscription_channel": candidate["subscription_channel"],
            "subscription_limit_cny": None if candidate["subscription_limit_cny"] is None else str(candidate["subscription_limit_cny"]),
            "subscription_eligible": subscription_eligible,
            "subscription_missing": subscription_missing,
        }
        results.append(result)
        if code == target_code:
            result.update(status="benchmark", reason="TARGET", missing=target_missing)
            continue
        if mode == "market" and code in exclude_codes:
            result.update(status="out_of_scope", reason="EXPLICITLY_EXCLUDED")
            continue
        if purity_eligible is False:
            result.update(status="excluded", reason="NOT_PURE_BOND")
            continue
        if anomaly_reviews[code]["status"] == "confirmed":
            result.update(status="excluded", reason="ABNORMAL_ANNUAL_RETURN")
            continue
        if result["nonpositive_annual_returns_pct"]:
            result["witnesses"].append({
                "rule": "R4", "nonpositive_annual_returns_pct": result["nonpositive_annual_returns_pct"],
                "sources": candidate["sources"], "data_completion": candidate["data_completion"],
            })
            result.update(status="excluded", reason="NONPOSITIVE_ANNUAL_RETURN")
            continue
        if subscription_eligible is False:
            result.update(status="excluded", reason="CURRENTLY_NOT_SUBSCRIBABLE")
            continue
        if assets_eligible is False:
            result.update(status="excluded", reason="FUND_ASSETS_BELOW_MINIMUM", fund_assets_shortfall_cny=str(minimum_assets - candidate["fund_assets_cny"]))
            continue
        if (
            candidate["ytd"] is not None and target["ytd"] is not None
            and comparable_history(candidate) and comparable_history(target)
            and anomaly_reviews[target_code]["status"] == "clear"
            and anomaly_reviews[code]["status"] == "clear"
        ):
            gap = target["ytd"] - candidate["ytd"]
            if gap >= target_ytd_gap:
                result["witnesses"].append({
                    "rule": "R3", "reference_code": target_code,
                    "ytd_gap_pp": str(gap), "threshold_pp": str(target_ytd_gap),
                })
        for reference_code in reference_codes:
            if reference_code in reference_conflict_missing:
                continue
            if reference_code == code:
                continue
            if (
                anomaly_reviews[target_code]["status"] != "clear"
                or anomaly_reviews[code]["status"] != "clear"
                or anomaly_reviews[reference_code]["status"] != "clear"
            ):
                continue
            reference = funds[reference_code]
            if three is not None and five is not None:
                reference_three = annualized(reference, three_years)
                reference_five = annualized(reference, five_years)
                if reference_three is not None and reference_five is not None and reference_three > three and reference_five > five:
                    result["ranking_only_dominators"].append(reference_code)
            if not complete_history(candidate, hard_years) or not complete_history(reference, hard_years):
                continue
            differences = {str(year): candidate["annual_returns"][year] - reference["annual_returns"][year] for year in hard_years}
            if all(value < 0 for value in differences.values()):
                result["witnesses"].append({
                    "rule": "R1", "reference_code": reference_code,
                    "annual_advantages_pp": {year: str(value) for year, value in differences.items()},
                })
            if candidate["ytd"] is not None and reference["ytd"] is not None:
                gap = reference["ytd"] - candidate["ytd"]
                maximum = max(differences.values())
                if gap >= Decimal("0.50") and maximum <= Decimal("0.20"):
                    result["witnesses"].append({
                        "rule": "R2", "reference_code": reference_code,
                        "ytd_gap_pp": str(gap), "max_annual_advantage_pp": str(maximum),
                        "annual_advantages_pp": {year: str(value) for year, value in differences.items()},
                    })
        if result["witnesses"]:
            result.update(status="excluded", reason="HARD_RULE")
            continue
        missing = list(target_missing)
        missing.extend(assets_missing)
        missing.extend(subscription_missing)
        missing.extend(purity_missing)
        if anomaly_reviews[code]["status"] != "clear":
            missing.append("ANNUAL_ANOMALY_REVIEW_UNCONFIRMED")
        if purity_eligible is None and "PURE_BOND_UNCONFIRMED" not in missing:
            missing.append("PURE_BOND_UNCONFIRMED")
        if not comparable_history(candidate):
            missing.append("PROXY_NOT_OWN_HISTORY")
        if not complete_history(candidate, hard_years):
            missing.append("HARD_YEAR_HISTORY_INCOMPLETE")
        if not complete_history(candidate, positive_years):
            missing.append("POSITIVE_ANNUAL_HISTORY_INCOMPLETE")
        if three is None or five is None:
            missing.append("LONG_TERM_HISTORY_INCOMPLETE")
        if candidate["ytd"] is None:
            missing.append("YTD_MISSING")
        if missing:
            result.update(status="observation", reason="DATA_OR_ELIGIBILITY_INCOMPLETE", missing=missing)
            continue
        if three < target_three and five < target_five:
            if candidate["ytd"] < floor:
                result.update(status="excluded", reason="BOTH_LOWER_YTD_BELOW_FLOOR")
            elif not any(Decimal(value) >= 0 for value in advantages.values()):
                result.update(status="excluded", reason="BOTH_LOWER_ALL_YEARS_BELOW_TARGET")
            else:
                result.update(status="qualified", reason="BOTH_LOWER_RECENT_EXCEPTION")
        elif three >= target_three and five >= target_five:
            result.update(status="qualified", reason="BOTH_HORIZONS_AT_LEAST_TARGET")
        else:
            result.update(status="qualified", reason="ONE_HORIZON_AT_LEAST_TARGET")
    if check_conflicts:
        check_return_disclosures(data, funds, results, earliest_year, anomaly_gap, target_override, mode_override, history_fill_groups)
    for result in results:
        if result["status"] == "qualified" and result["disclosure_notes"]:
            result["retained_with_notes"] = True
    qualified = sorted(
        [result for result in results if result["status"] == "qualified"],
        key=lambda result: qualified_sort_key({**result, "ytd": funds[result["code"]]["ytd"]}),
    )
    results = (
        [result for result in results if result["status"] == "benchmark"]
        + qualified
        + [result for result in results if result["status"] not in ("benchmark", "qualified")]
    )
    return {
        "as_of": as_of, "screen_date": screen_date, "mode": mode, "target_code": target_code, "target_source": target_source,
        "min_fund_assets_cny": str(minimum_assets),
        "annual_anomaly_gap_pp": str(anomaly_gap),
        "target_ytd_gap_pp": str(target_ytd_gap),
        "earliest_screen_year": earliest_year,
        "annual_anomaly_mean_basis": "other_complete_calendar_years_excluding_tested_year",
        "exclude_codes": exclude_codes, "target_data_unavailable": target_missing,
        "calendar_3y": three_years, "calendar_5y": five_years,
        "hard_years": hard_years, "reference_codes": reference_codes,
        "positive_annual_years": positive_years,
        "reference_history_unavailable": reference_missing,
        "reference_ytd_unavailable": reference_ytd_missing,
        "reference_anomaly_unavailable": reference_anomaly_missing,
        "reference_return_conflict_unavailable": reference_conflict_missing,
        "history_fill_enabled": history_fill_enabled,
        "same_fund_data_completion": {
            code: fund["data_completion"] for code, fund in funds.items() if fund["data_completion"]
        },
        "thresholds_pp": {"R2_ytd_gap": "0.50", "R2_max_annual_advantage": "0.20", "R3_ytd_gap": str(target_ytd_gap), "both_lower_ytd_tolerance": "0.30"},
        "both_lower_ytd_floor": str(floor) if floor is not None else None,
        "qualified_output_order": {
            "groups": list(QUALIFIED_GROUP_ORDER), "within_group": "ytd_desc",
            "tie_breaker": "code_asc", "ytd_basis": "unrounded",
        },
        "return_screen_only": True, "results": results,
    }


def main():
    parser = argparse.ArgumentParser(description="按份额实际及注明来源的同基金补全收益筛选纯债基金；不联网、不交易")
    parser.add_argument("input", help="符合references/data-contract.md的JSON文件")
    parser.add_argument("--target-code", help="本次目标基金代码，优先于输入和预制默认目标")
    parser.add_argument("--mode", choices=("market", "table"), help="本次筛选模式，默认使用预设market")
    args = parser.parse_args()
    try:
        with open(args.input, encoding="utf-8") as handle:
            data = json.load(handle, parse_float=Decimal)
        print(json.dumps(screen(data, target_override=args.target_code, mode_override=args.mode), ensure_ascii=False, indent=2))
    except (ValueError, KeyError, TypeError, OSError, InvalidOperation) as error:
        print(f"输入或计算失败: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
