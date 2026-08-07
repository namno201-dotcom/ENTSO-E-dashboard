#!/usr/bin/env python3
"""
fetch_entsog.py
Pulls physical gas pipeline flow data from the ENTSOG Transparency Platform for
the main entry points where Norwegian, Algerian and Azerbaijani gas enters the
European network. Writes data/gas_flows_daily.json for the dashboard to read.

No API key needed for ENTSOG (unlike ENTSO-E). Unlike ENTSO-E's XML API, ENTSOG
returns clean daily JSON rows directly -- no forward-fill or month-chunking needed.
"""

import os
import sys
import json
import time
import datetime as dt

import requests

BASE_URL = "https://transparency.entsog.eu/api/v1/operationaldatas.json"
START_DATE = dt.date(2026, 1, 1)
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

POINTS = [
    {"origin": "Norway", "label": "St Fergus (UK)", "operatorKey": "UK-TSO-0001", "pointKey": "ITP-00022"},
    {"origin": "Norway", "label": "Easington (UK)", "operatorKey": "UK-TSO-0001", "pointKey": "ITP-00091"},
    {"origin": "Norway", "label": "Dunkerque (FR)", "operatorKey": "FR-TSO-0003", "pointKey": "ITP-00045"},
    {"origin": "Norway", "label": "Zeebrugge (BE)", "operatorKey": "BE-TSO-0001", "pointKey": "ITP-00106"},
    {"origin": "Norway", "label": "Emden EPT1 (DE)", "operatorKey": "DE-TSO-0005", "pointKey": "ITP-00081"},
    {"origin": "Algeria", "label": "Almeria (ES) - Medgaz", "operatorKey": "ES-TSO-0006", "pointKey": "ITP-00048"},
    {"origin": "Algeria", "label": "Mazara del Vallo (IT) - Transmed", "operatorKey": "IT-TSO-0001", "pointKey": "ITP-00093"},
    {"origin": "Azerbaijan", "label": "Kipoi (GR) - TAP/TANAP", "operatorKey": "AL-TSO-0001", "pointKey": "ITP-00274"},
]

SESSION = requests.Session()


def fetch_point(point, start: dt.date, end: dt.date, retries: int = 4):
    params = {
        "operatorKey": point["operatorKey"],
        "pointKey": point["pointKey"],
        "directionKey": "entry",
        "indicator": "Physical Flow",
        "periodType": "day",
        "from": start.isoformat(),
        "to": end.isoformat(),
        "limit": 1000,
    }
    for attempt in range(1, retries + 1):
        try:
            resp = SESSION.get(BASE_URL, params=params, timeout=60)
        except requests.RequestException as exc:
            print(f"  ! network error for {point['label']} ({exc}), retry {attempt}/{retries}")
            time.sleep(3 * attempt)
            continue
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429:
            print(f"  ! rate limited for {point['label']}, retry {attempt}/{retries}")
            time.sleep(10 * attempt)
            continue
        print(f"  ! HTTP {resp.status_code} for {point['label']} -- {resp.text[:200]}")
        return None
    return None


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    today = dt.datetime.now(dt.timezone.utc).date()

    rows = []
    for point in POINTS:
        print(f"Fetching {point['origin']} / {point['label']}")
        payload = fetch_point(point, START_DATE, today)
        if not payload:
            continue
        meta = payload.get("meta", {})
        total = meta.get("total", 0)
        count = meta.get("count", 0)
        if total and count and total > count:
            print(f"  ! warning: {point['label']} returned {count} of {total} rows -- consider paging")

        for rec in payload.get("operationaldatas", []):
            raw_value = rec.get("value")
            if raw_value in (None, "", "NA"):
                continue
            try:
                value_kwh_d = float(raw_value)
            except (TypeError, ValueError):
                continue
            period_from = rec.get("periodFrom")
            if not period_from:
                continue
            date_str = period_from[:10]
            rows.append({
                "origin": point["origin"],
                "label": point["label"],
                "date": date_str,
                "gwhPerDay": round(value_kwh_d / 1_000_000, 3),
            })
        time.sleep(0.3)

    rows.sort(key=lambda r: (r["origin"], r["label"], r["date"]))

    totals = {}
    for r in rows:
        key = (r["origin"], r["date"])
        totals[key] = totals.get(key, 0.0) + r["gwhPerDay"]
    origin_daily = [
        {"origin": origin, "date": date, "gwhPerDay": round(v, 3)}
        for (origin, date), v in sorted(totals.items())
    ]

    with open(os.path.join(DATA_DIR, "gas_flows_points_daily.json"), "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2)
    with open(os.path.join(DATA_DIR, "gas_flows_daily.json"), "w", encoding="utf-8") as f:
        json.dump(origin_daily, f, indent=2)

    print(f"Done. {len(rows)} point-day rows, {len(origin_daily)} origin-day totals.")


if __name__ == "__main__":
    main()
