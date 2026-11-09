import hashlib
import json
from pathlib import Path

from market_cache import fingerprint
from market_data import save_json


def canonical(value, key=None):
    if isinstance(value, dict):
        return {name: canonical(content, name) for name, content in value.items()}
    if isinstance(value, list):
        values = [canonical(content) for content in value]
        if key in ("funds", "results"):
            values.sort(key=lambda content: content["code"])
        elif key == "history_fill_groups":
            values.sort(key=lambda content: tuple(content["codes"]))
        elif key in ("codes", "sources", "missing", "exclude_codes", "reference_codes"):
            values.sort()
        return values
    return value


def build_audit(root, data, output):
    paths = ["SKILL.md", "references/defaults.json", "scripts/screen_funds.py", "scripts/disclosures.py", "scripts/market_data.py", "scripts/market_cache.py", "scripts/run_market.py", "scripts/decision_audit.py"]
    versions = {path: hashlib.sha256((root / path).read_bytes()).hexdigest() for path in paths}
    inputs = {fund["code"]: fund for fund in data["funds"]}
    performance_fields = ("data_date", "annual_returns", "ytd", "recent_6m", "recent_3m")
    return {"as_of": data["as_of"], "screen_date": data["screen_date"], "target_code": output["target_code"], "policy_and_code_sha256": fingerprint(versions), "versions": versions, "input_sha256": fingerprint(canonical(data)), "result_sha256": fingerprint(canonical(output)), "fund_input_sha256": {code: fingerprint(canonical(fund)) for code, fund in inputs.items()}, "fund_performance_sha256": {code: fingerprint(canonical({field: fund.get(field) for field in performance_fields})) for code, fund in inputs.items()}, "fund_decisions": {fund["code"]: {"status": fund["status"], "reason": fund.get("reason"), "missing": sorted(fund.get("missing", [])), "verification_issues": inputs[fund["code"]].get("verification_issues", [])} for fund in output["results"]}, "previous_results_used_for_selection": False}


def write_audit(root, destination, output_dir, data, output):
    audit = build_audit(root, data, output)
    for result in output["results"]:
        audit["fund_decisions"][result["code"]].update(retained_with_notes=result.get("retained_with_notes", False), disclosure_notes=result.get("disclosure_notes", []), return_source_sensitivity=result.get("return_source_sensitivity"))
    latest = output_dir / "latest.json"
    if latest.exists():
        try:
            previous_directory = Path(json.loads(latest.read_text())["run_directory"]).resolve()
            if previous_directory.is_relative_to(output_dir.resolve()):
                previous = json.loads((previous_directory / "decision_audit.json").read_text())
                comparable = previous.get("policy_and_code_sha256") == audit["policy_and_code_sha256"] and previous.get("input_sha256") == audit["input_sha256"]
                audit["previous_comparison"] = {"run_directory": str(previous_directory), "same_policy_and_code": previous.get("policy_and_code_sha256") == audit["policy_and_code_sha256"], "same_input": previous.get("input_sha256") == audit["input_sha256"], "same_result": previous.get("result_sha256") == audit["result_sha256"], "comparison_only": True}
                audit["fund_decision_changes"] = []
                previous_decisions = previous.get("fund_decisions", {})
                for code in sorted(set(previous_decisions) | set(audit["fund_decisions"])):
                    before = previous_decisions.get(code)
                    after = audit["fund_decisions"].get(code)
                    if before != after:
                        previous_input = previous.get("fund_input_sha256", {}).get(code)
                        previous_performance = previous.get("fund_performance_sha256", {}).get(code)
                        audit["fund_decision_changes"].append({"code": code, "before": before, "after": after, "fund_input_changed": previous_input != audit["fund_input_sha256"].get(code) if previous_input else None, "fund_performance_changed": previous_performance != audit["fund_performance_sha256"].get(code) if previous_performance else None})
                if comparable and previous.get("result_sha256") != audit["result_sha256"]:
                    audit["determinism_violation"] = True
        except (ValueError, KeyError, TypeError, OSError):
            audit["previous_comparison_unavailable"] = True
    save_json(destination / "decision_audit.json", audit)
    if audit.get("determinism_violation"):
        raise ValueError("同一规则版本及同一完整输入产生不同结果，停止发布，详见decision_audit.json")
    return audit
