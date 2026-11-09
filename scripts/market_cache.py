import hashlib
import json
import sqlite3
import threading
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def closed_day(day, calendar_file=None):
    if day.weekday() >= 5:
        return True
    if calendar_file is None:
        calendar_file = Path(__file__).resolve().parent.parent / "references" / "trading-calendar.json"
    calendar = json.loads(Path(calendar_file).read_text(encoding="utf-8"))
    if not isinstance(calendar.get("source"), str) or not calendar["source"] or not isinstance(calendar.get("holidays"), list):
        raise ValueError("交易所日历须包含source及holidays日期列表")
    holidays = {date.fromisoformat(value) for value in calendar["holidays"]}
    return day in holidays


class MarketCache:
    def __init__(self, directory, timestamp, force=False):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.timestamp = timestamp
        self.force = force
        self.lock = threading.RLock()
        self.events = []
        self.active_codes = None
        self.cutoff = None
        self.connection = sqlite3.connect(directory / "market.sqlite3", check_same_thread=False)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute("CREATE TABLE IF NOT EXISTS records (layer TEXT, key TEXT, payload TEXT, digest TEXT, version TEXT, fetched_at TEXT, data_date TEXT, status TEXT, PRIMARY KEY(layer, key))")
        self.connection.commit()

    def event(self, layer, key, action, **details):
        with self.lock:
            self.events.append({"layer": layer, "key": key, "action": action, **details})

    def load(self, layer, key, version, max_age, data_date=None):
        with self.lock:
            if self.force:
                self.event(layer, key, "forced")
                return None
            record = self.connection.execute("SELECT payload, digest, version, fetched_at, data_date, status FROM records WHERE layer=? AND key=?", (layer, key)).fetchone()
            if record is None:
                self.event(layer, key, "missing")
                return None
            payload, digest, stored_version, fetched_at, stored_date, status = record
            try:
                value = json.loads(payload)
                age = self.timestamp - datetime.fromisoformat(fetched_at)
                valid = (status == "complete" and stored_version == version and fingerprint(value) == digest
                         and timedelta(0) <= age <= max_age and (data_date is None or stored_date == data_date))
            except (ValueError, TypeError):
                valid = False
            if not valid:
                self.event(layer, key, "invalid", data_date=stored_date, original_fetched_at=fetched_at)
                return None
            self.event(layer, key, "reused", data_date=stored_date, original_fetched_at=fetched_at, sha256=digest)
            return value

    def store(self, layer, key, value, version, complete=True, data_date=None):
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = fingerprint(value)
        with self.lock:
            previous = self.connection.execute("SELECT digest, data_date, fetched_at, payload, status FROM records WHERE layer=? AND key=?", (layer, key)).fetchone()
            if previous and (datetime.fromisoformat(previous[2]) > self.timestamp or (layer == "phase" and previous[1] and data_date and previous[1] > data_date)):
                self.event(layer, key, "older_observation_ignored", data_date=data_date)
                return
            if layer == "phase" and not complete and not self.force and previous and previous[1] == data_date and previous[4] == "complete":
                try:
                    old_value = json.loads(previous[3])
                    compatible = fingerprint(old_value) == previous[0] and all(old_value.get(field) == observed for field, observed in value.items() if observed is not None)
                except (ValueError, TypeError):
                    compatible = False
                if compatible:
                    self.event(layer, key, "partial_observation_ignored", data_date=data_date)
                    return
            status = "complete" if complete else "pending"
            self.connection.execute("INSERT OR REPLACE INTO records VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (layer, key, payload, digest, version, self.timestamp.isoformat(), data_date, status))
            self.connection.commit()
            action = "pending" if not complete else "unchanged" if previous and previous[0] == digest else "updated"
            self.event(layer, key, action, data_date=data_date, sha256=digest, fetched_at=self.timestamp.isoformat())

    def invalidate(self, layer, key):
        with self.lock:
            self.connection.execute("UPDATE records SET status='pending' WHERE layer=? AND key=?", (layer, key))
            self.connection.commit()
            self.event(layer, key, "retry_required")

    def fetch(self, layer, key, version, loader, validator=lambda value: True, max_age=timedelta(days=7), data_date=None):
        value = self.load(layer, key, version, max_age, data_date)
        if value is not None:
            return value
        try:
            value = loader()
            complete = validator(value)
            observed_date = value.get("data_date") if isinstance(value, dict) else None
            self.store(layer, key, value, version, complete, observed_date)
            return value
        except Exception:
            self.invalidate(layer, key)
            self.event(layer, key, "failed")
            raise

    def audit(self):
        with self.lock:
            counts = {}
            for event in self.events:
                counts.setdefault(event["layer"], Counter())[event["action"]] += 1
            records = {}
            for code, data_date, status, fetched_at, digest in self.connection.execute("SELECT key, data_date, status, fetched_at, digest FROM records WHERE layer='phase'"):
                if self.active_codes is None or code in self.active_codes:
                    records[code] = {"data_date": data_date, "status": status, "original_fetched_at": fetched_at, "sha256": digest, "needs_refresh": status != "complete" or (self.cutoff is not None and data_date != self.cutoff)}
            for code in self.active_codes or []:
                records.setdefault(code, {"data_date": None, "status": "missing", "needs_refresh": True})
            dates = Counter(record["data_date"] or "missing" for record in records.values())
            return {"checked_at": self.timestamp.isoformat(), "as_of": self.cutoff, "force_refresh": self.force, "counts": counts, "phase_dates": dates, "phase_records": records, "events": list(self.events)}

    def close(self):
        self.connection.close()


def phase_from_row(row):
    from market_data import percent

    try:
        date.fromisoformat(row[3])
        return {"data_date": row[3], "ytd": percent(row[14]), "recent_6m": percent(row[10]), "recent_3m": percent(row[9])}
    except (ValueError, IndexError, TypeError):
        return None


def complete_phase(value):
    return isinstance(value, dict) and all(value.get(field) is not None for field in ("data_date", "ytd", "recent_6m", "recent_3m"))


def complete_annual(value, inception, end_year):
    start = date.fromisoformat(inception)
    own_years = range(max(2020, start.year + (start.month != 1 or start.day != 1)), end_year + 1)
    returns = value.get("annual_returns", {})
    return all(returns.get(str(year), returns.get(year)) is not None for year in own_years)
