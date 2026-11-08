import argparse
import copy
import csv
import json
import re
import sys
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path

import screen_funds as engine
from disclosures import review_family
from market_data import Client, annual, metadata, now, parallel, parse_catalog, parse_phase, parse_subscription, percent, ranking, save_json


ROOT = Path(__file__).resolve().parent.parent
GROUP_TITLES = dict(zip(engine.QUALIFIED_GROUP_ORDER, ("三年、五年均不低于目标", "至少一项不低于目标", "长期两项较低、今年表现例外")))


def family_key(fund):
    return fund["full_name"], fund["manager"], fund["currency"]


def provisional_groups(funds):
    families = defaultdict(list)
    for fund in funds.values():
        if fund.get("full_name") and fund.get("manager") and fund.get("currency"):
            families[family_key(fund)].append(fund)
    groups = []
    for members in families.values():
        if len(members) > 1:
            groups.append({"codes": sorted(fund["code"] for fund in members), "preferred_codes": [fund["code"] for fund in sorted(members, key=lambda fund: (not fund["name"].endswith("A"), fund["inception_date"], fund["code"]))], "sources": [fund["identity_source"] for fund in members]})
    return groups, families


def numeric_screen(raw, groups, target_code, defaults, as_of):
    funds = {code: engine.normalize_fund(fund, as_of) for code, fund in raw.items()}
    end_year = int(as_of[:4]) - 1
    hard_years = defaults["hard_years"]
    complete_years = list(range(defaults["earliest_screen_year"], end_year + 1))
    three_years = list(range(end_year - 2, end_year + 1))
    five_years = list(range(end_year - 4, end_year + 1))
    if min(five_years) < defaults["earliest_screen_year"]:
        raise ValueError("2020年起的数据不足完整五年，不能缩短窗口")
    engine.complete_same_fund_data(funds, groups, True, sorted(set(complete_years + hard_years + five_years)))
    target = funds[target_code]
    target_three = engine.annualized(target, three_years)
    target_five = engine.annualized(target, five_years)
    if target["ytd"] is None or target_three is None or target_five is None or not engine.complete_history(target, hard_years):
        raise ValueError("目标缺少当前收益或完整三五年历史，停止筛选，不更换目标")
    statuses = {}
    for code, fund in funds.items():
        if code == target_code:
            statuses[code] = "benchmark"
            continue
        if fund["ytd"] is not None and target["ytd"] - fund["ytd"] >= Decimal(defaults["target_ytd_gap_pp"]):
            statuses[code] = "R3_TARGET_YTD_GAP"
            continue
        anomalies = engine.quantitative_annual_anomalies(fund, Decimal(defaults["annual_anomaly_gap_pp"]), defaults["earliest_screen_year"])
        if anomalies["status"] == "confirmed":
            statuses[code] = "ANNUAL_ANOMALY"
            continue
        if engine.negative_annual_returns(fund, complete_years):
            statuses[code] = "NEGATIVE_ANNUAL_RETURN"
            continue
        if not engine.complete_history(fund, complete_years + hard_years) or fund["ytd"] is None:
            statuses[code] = "HISTORY_OR_YTD_UNVERIFIED"
            continue
        advantages = [fund["annual_returns"][year] - target["annual_returns"][year] for year in hard_years]
        if all(advantage < 0 for advantage in advantages):
            statuses[code] = "R1_DOMINATED_ALL_YEARS"
            continue
        if target["ytd"] - fund["ytd"] >= Decimal("0.50") and max(advantages) <= Decimal("0.20"):
            statuses[code] = "R2_DOMINATED_WITH_YTD_GAP"
            continue
        candidate_three = engine.annualized(fund, three_years)
        candidate_five = engine.annualized(fund, five_years)
        if candidate_three is None or candidate_five is None:
            statuses[code] = "HISTORY_OR_YTD_UNVERIFIED"
        elif candidate_three >= target_three or candidate_five >= target_five or (fund["ytd"] >= target["ytd"] - Decimal("0.30") and max(advantages) >= 0):
            statuses[code] = "numeric_candidate"
        else:
            statuses[code] = "LONG_AND_RECENT_BELOW_TARGET"
    return statuses


def representatives(output, raw, groups, target_code):
    owners = {code: tuple(group["codes"]) for group in groups for code in group["codes"]}
    families = defaultdict(list)
    for result in output["results"]:
        if result["status"] == "qualified":
            families[owners.get(result["code"], (result["code"],))].append(result)
    selected = set()
    for members in families.values():
        if any(target_code in owners.get(member["code"], ()) for member in members):
            continue
        choice = min(members, key=lambda result: (result["code"] != "015736" if "015736" in owners.get(result["code"], ()) else not result["name"].endswith("A"), -Decimal(result["cagr5"] or "-100"), -Decimal(result["cagr3"] or "-100"), result["code"]))
        selected.add(choice["code"])
    return [{**result, **{field: raw[result["code"]].get(field) for field in ("fund_assets_cny", "fund_assets_date", "subscription_status", "subscription_limit_cny", "pure_bond_basis")}} for result in output["results"] if result["code"] in selected]


def export_report(destination, output, raw, groups, coverage, top_lists):
    retained = representatives(output, raw, groups, output["target_code"])
    save_json(destination / "retained_funds.json", retained)
    fields = ["code", "name", "reason", "ytd", "cagr3", "cagr5", "recent_6m", "recent_3m", "fund_assets_cny", "fund_assets_date", "subscription_status", "subscription_limit_cny", "pure_bond_basis"]
    with (destination / "retained_funds.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(retained)
    benchmark = next(result for result in output["results"] if result["status"] == "benchmark")
    lines = ["# 纯债全市场筛选", "", f"执行时间：{coverage['started_at']}；净值共同截止日：{output['as_of']}；目标：{benchmark['code']} {benchmark['name']}", f"市场目录：{coverage['catalog_bond_share_classes']}份额；正式入选：{len(retained)}只（同基金去重）。", f"三年完整年度：{output['calendar_3y']}；五年完整年度：{output['calendar_5y']}。", f"基准今年 {benchmark['ytd']}%，三年 {benchmark['cagr3']}%，五年 {benchmark['cagr5']}%。", "", "同类按未四舍五入的今年收益降序；限购额度为当前天天基金渠道状态。"]
    for reason, title in GROUP_TITLES.items():
        members = [result for result in retained if result["reason"] == reason]
        lines.extend(["", f"## {title}（{len(members)}只）", "|代码|基金|今年%|三年年化%|五年年化%|近半年%|近3个月%|总规模亿元|规模报告日|申购/限额|", "|---|---|---:|---:|---:|---:|---:|---:|---|---|"])
        for result in members:
            size = str((Decimal(result["fund_assets_cny"]) / Decimal(100000000)).quantize(Decimal("0.0001")))
            trade = "开放" if result["subscription_status"] == "open" else f"限购 {result['subscription_limit_cny']}元/日"
            name = result["name"] + ("†" if result["pure_bond_basis"] == "actual_holdings" else "")
            values = [result["code"], name] + [result.get(field) or "缺失" for field in ("ytd", "cagr3", "cagr5", "recent_6m", "recent_3m")] + [size, result["fund_assets_date"], trade]
            lines.append("|" + "|".join(str(value).replace("|", "／") for value in values) + "|")
    observations = [result for result in output["results"] if result["status"] == "observation"]
    lines.extend(["", f"## 待核实（{len(observations)}份额，不正式入选）"])
    for result in observations:
        lines.append(f"- {result['code']} {result['name']}：{', '.join(result.get('missing', []))}")
    lines.extend(["", "## 覆盖与风险", f"- 覆盖状态：{coverage['status']}；源数据/解析失败 {coverage['unresolved_count']} 项，详见 coverage.json 和 market_audit.json，不宣称穷尽无法核实的市场。", "- 当前公开净值不等于今天实时估值；新份额历史仅以有来源的同基金份额补全。", "- †最新披露实际持仓符合纯债；合同仍可能允许非纯债资产，持仓有滞后。", "- 全年收益和异常核查仅考虑2020年起；负收益、规模和交易限制不因长期优势豁免。", "- 本结果只供研究；纯债不保本，需另查久期、信用、杠杆、回撤、费用及机构持有人变化。"])
    if top_lists:
        lists = {}
        for field, title in [("ytd", "今年"), ("recent_6m", "近半年"), ("recent_3m", "近3个月")]:
            members = [result for result in retained if result.get(field) is not None]
            members.sort(key=lambda result: (Decimal(raw[result["code"]].get(field) or result.get("data_completion", {}).get(field, {}).get("return_pct") or result[field]).copy_negate(), result["code"]))
            lists[field] = members[:5]
            lines.extend(["", f"## {title}前5只（不足留空）"])
            lines.extend(f"- {result['code']} {result['name']}：{result[field]}%" for result in members[:5])
        save_json(destination / "top_lists.json", lists)
    (destination / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return retained


def run(args, destination, client, defaults):
    today = now().date()
    today_text = today.isoformat()
    target_code = args.target_code or defaults["target_code"]
    if not re.fullmatch(r"\d{6}", target_code):
        raise ValueError("目标必须为六位基金代码")
    catalog = parse_catalog(client.get("https://fund.eastmoney.com/js/fundcode_search.js"))
    save_json(destination / "catalog.json", list(catalog.values()))
    if target_code not in catalog:
        raise ValueError("指定目标不在当前债基目录中，停止，不回退预制目标")
    ranks = ranking(client, "zq")
    mainland_count = len(ranks)
    qdii = ranking(client, "qdii")
    ranks.update({code: row for code, row in qdii.items() if code in catalog})
    save_json(destination / "ranking.json", ranks)
    target_row = ranks.get(target_code)
    if not target_row or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", target_row[3]):
        phase = parse_phase(client.get(f"https://fund.eastmoney.com/{target_code}.html"))
        as_of = phase["data_date"]
    else:
        as_of = target_row[3]
    cutoff = date.fromisoformat(as_of)
    if cutoff > today or (today - cutoff).days > 14 or cutoff.year != today.year:
        raise ValueError("目标最新净值过期/跨年或晚于今天，不能冒充当前今年收益")
    dates = [row[3] for code, row in ranks.items() if code in catalog and re.fullmatch(r"\d{4}-\d{2}-\d{2}", row[3]) and row[3] <= today_text]
    if dates and max(dates) > as_of:
        raise ValueError("目标净值落后市场更新节奏，不能用旧目标日期筛选最新市场")
    gap_codes = sorted(code for code in catalog if code not in ranks or ranks[code][3] != as_of)
    gaps = parallel(gap_codes, lambda code: parse_phase(client.get(f"https://fund.eastmoney.com/{code}.html")), args.workers, "目录差集阶段收益")
    identities = parallel(sorted(catalog), lambda code: metadata(client, code), args.workers, "全市场份额身份")
    save_json(destination / "identities.json", identities)
    raw = {}
    failures = []
    for code, entry in catalog.items():
        identity = identities[code]
        if identity["status"] != "ok":
            failures.append({"code": code, "stage": "identity", **identity})
            continue
        source = f"https://fund.eastmoney.com/{code}.html"
        if code not in gaps:
            row = ranks[code]
            phase = {"data_date": as_of, "ytd": percent(row[14]), "recent_6m": percent(row[10]), "recent_3m": percent(row[9])}
        elif gaps[code]["status"] == "ok" and gaps[code]["data"]["data_date"] == as_of:
            phase = gaps[code]["data"]
        else:
            failures.append({"code": code, "stage": "phase", **gaps[code]})
            phase = {"data_date": as_of, "ytd": None, "recent_6m": None, "recent_3m": None}
        raw[code] = {"code": code, "name": entry[2], "classification": entry[3], "history_basis": "share_class", "pure_bond": None, "annual_returns": {}, "sources": [source, identity["data"]["identity_source"]], **identity["data"], **phase}
    if target_code not in raw or raw[target_code]["ytd"] is None:
        raise ValueError("目标当前收益/身份数据缺失，停止筛选")
    groups, families = provisional_groups(raw)
    target_ytd = Decimal(raw[target_code]["ytd"])
    annual_codes = set()
    early_audit = {}
    for members in families.values():
        if any(fund["code"] == target_code or fund["ytd"] is None or target_ytd - Decimal(fund["ytd"]) < Decimal(defaults["target_ytd_gap_pp"]) for fund in members):
            annual_codes.update(fund["code"] for fund in members)
        else:
            early_audit.update({fund["code"]: "R3_TARGET_YTD_GAP" for fund in members})
    annuals = parallel(sorted(annual_codes), lambda code: annual(client, code, raw[code]["inception_date"], cutoff.year - 1), args.workers, "当前年度历史")
    save_json(destination / "annuals.json", annuals)
    for code, result in annuals.items():
        if result["status"] == "ok":
            raw[code]["annual_returns"] = result["data"]["annual_returns"]
            raw[code]["sources"].append(result["data"]["annual_source"])
        else:
            failures.append({"code": code, "stage": "annual", **result})
    active = {code: fund for code, fund in raw.items() if code in annual_codes}
    relevant_groups = [group for group in groups if set(group["codes"]).intersection(active)]
    numeric = numeric_screen(active, relevant_groups, target_code, defaults, as_of)
    save_json(destination / "fresh_funds.json", list(active.values()))
    save_json(destination / "numeric_audit.json", {"early": early_audit, "numeric": numeric, "failures": failures})
    job_keys = [key for key, members in families.items() if any(numeric.get(fund["code"]) in ("numeric_candidate", "benchmark") for fund in members)]
    reviews = parallel(job_keys, lambda key: review_family(client, [fund for fund in families[key] if fund["code"] in active], args.output_dir / ".cache" / "documents", today_text, as_of, defaults["earliest_screen_year"]), args.workers, "管理人完整报告核查")
    save_json(destination / "family_reviews.json", {" / ".join(key): value for key, value in reviews.items()})
    reviewed = {}
    verified_groups = []
    for key, result in reviews.items():
        members = [fund for fund in families[key] if fund["code"] in active]
        if result["status"] != "ok":
            failures.append({"family": list(key), "stage": "disclosure", **result})
            review = {}
        else:
            review = result["data"]
        verified_codes = set(review.get("verified_codes", []))
        if len(verified_codes) > 1:
            verified_groups.append({"codes": sorted(verified_codes), "preferred_codes": [fund["code"] for fund in sorted(members, key=lambda fund: (not fund["name"].endswith("A"), fund["inception_date"], fund["code"])) if fund["code"] in verified_codes], "sources": [review["latest_report"]["source"]]})
        for fund in members:
            candidate = copy.deepcopy(fund)
            if fund["code"] in verified_codes:
                candidate.update({field: review[field] for field in ("pure_bond", "pure_bond_basis", "pure_bond_review", "fund_assets_cny", "fund_assets_date", "fund_assets_basis", "fund_assets_sources", "annual_anomaly_review") if field in review})
                candidate["sources"].append(review["latest_report"]["source"])
            reviewed[fund["code"]] = candidate
    if target_code not in reviewed or reviewed[target_code]["pure_bond"] is not True:
        save_json(destination / "market_audit.json", {"early": early_audit, "numeric": numeric, "failures": failures})
        raise ValueError("目标官方纯债属性/最新报告未确认，停止正式筛选，详见family_reviews.json")
    trades = parallel(sorted(reviewed), lambda code: parse_subscription(client.get(f"https://fund.eastmoney.com/{code}.html"), today_text), args.workers, "当前申购状态")
    for code, result in trades.items():
        if result["status"] == "ok":
            reviewed[code].update(result["data"])
            reviewed[code]["subscription_sources"] = [f"https://fund.eastmoney.com/{code}.html"]
        else:
            failures.append({"code": code, "stage": "subscription", **result})
    if now().date() != today:
        raise ValueError("执行跨过上海日期，需重新核实当前交易状态后重跑")
    data = {"as_of": as_of, "screen_date": today_text, "calendar_end_year": cutoff.year - 1, "mode": "market", "target_code": target_code, "reference_codes": [], "exclude_codes": [], "history_fill_groups": verified_groups, "funds": list(reviewed.values())}
    save_json(destination / "screen_input.json", data)
    output = engine.screen(data)
    save_json(destination / "screen_output.json", output)
    save_json(destination / "market_audit.json", {"early": early_audit, "numeric": numeric, "failures": failures})
    coverage = {"started_at": args.started_at, "completed_at": now().isoformat(), "as_of": as_of, "screen_date": today_text, "catalog_bond_share_classes": len(catalog), "rank_mainland_count": mainland_count, "ranking_records": len(ranks), "directory_gap_checks": len(gaps), "identity_success": len(raw), "annual_fresh_checks": len(annual_codes), "official_family_checks": len(reviews), "unresolved_count": len(failures), "status": "completed_with_gaps" if failures or output["target_data_unavailable"] else "completed", "fresh_market_directory": True, "previous_candidate_lists_used": False}
    save_json(destination / "coverage.json", coverage)
    retained = export_report(destination, output, reviewed, verified_groups, coverage, args.top_lists)
    if output["target_data_unavailable"]:
        raise ValueError("目标年度数据/异常核查尚未确认，不能产出有效正式筛选")
    save_json(args.output_dir / "latest.json", {"run_directory": str(destination), "report": str(destination / "report.md"), "as_of": as_of, "status": coverage["status"], "qualified_count": len(retained)})
    print(f"正式入选 {len(retained)}只；报告：{destination / 'report.md'}", flush=True)
    return 3 if failures else 0


def main():
    parser = argparse.ArgumentParser(description="从当前全市场债基目录一键筛选纯债，失败/缺证据不假装成功")
    parser.add_argument("--target-code", help="本次目标六位代码；省略固定400030，不持久化")
    parser.add_argument("--output-dir", type=Path, default=Path.cwd() / "bond-screen-results")
    parser.add_argument("--workers", type=int, choices=range(1, 9), default=6)
    parser.add_argument("--timeout", type=int, default=25)
    parser.add_argument("--top-lists", action="store_true", help="额外生成今年、半年、3个月各最多5只")
    args = parser.parse_args()
    if args.timeout < 1 or args.timeout > 120:
        parser.error("timeout必须在1—120秒")
    args.output_dir = args.output_dir.resolve()
    started = now()
    args.started_at = started.isoformat()
    destination = args.output_dir / started.strftime("%Y%m%d_%H%M%S_%f_market")
    destination.mkdir(parents=True)
    client = Client(destination / "evidence", timeout=args.timeout)
    defaults = json.loads((ROOT / "references" / "defaults.json").read_text())
    try:
        print(f"开始全市场筛选；目标 {args.target_code or defaults['target_code']}；输出 {destination}", flush=True)
        return run(args, destination, client, defaults)
    except KeyboardInterrupt:
        save_json(destination / "failure.json", {"status": "interrupted", "at": now().isoformat()})
        return 130
    except Exception as error:
        save_json(destination / "failure.json", {"status": "failed", "error": f"{type(error).__name__}: {error}", "at": now().isoformat()})
        print(f"筛选未完成：{error}；诊断：{destination}", file=sys.stderr)
        return 2
    finally:
        save_json(destination / "source_manifest.json", client.manifest)


if __name__ == "__main__":
    raise SystemExit(main())
