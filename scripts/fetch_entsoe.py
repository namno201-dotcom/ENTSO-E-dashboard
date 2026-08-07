#!/usr/bin/env python3
"""
fetch_entsoe.py
Pulls Load, Generation and Cross-border Flow data from the ENTSO-E Transparency
Platform REST API for Germany, Netherlands, Belgium and Luxembourg, and writes
pre-aggregated JSON files into ./data for the static dashboard (index.html) to read.

Runs from GitHub Actions on a schedule. Requires the ENTSOE_TOKEN secret/env var.
"""

import os
import sys
import time
import json
import datetime as dt
from collections import defaultdict
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import requests

TOKEN = os.environ.get("ENTSOE_TOKEN", "").strip()
if not TOKEN:
    print("ERROR: ENTSOE_TOKEN environment variable is not set.", file=sys.stderr)
    sys.exit(1)

BASE_URL = "https://web-api.tp.entsoe.eu/api"
LOCAL_TZ = ZoneInfo("Europe/Berlin")
START_DATE = dt.date(2026, 1, 1)
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

DOMAIN = {
    "DE": "10Y1001A1001A82H",
    "LU": "10YLU-CEGEDEL-NQ",
    "NL": "10YNL----------L",
    "BE": "10YBE----------2",
    "FR": "10YFR-RTE------C",
    "AT": "10YAT-APG------L",
    "CH": "10YCH-SWISSGRIDZ",
    "PL": "10YPL-AREA-----S",
    "CZ": "10YCZ-CEPS-----N",
    "DK1": "10YDK-1--------W",
    "GB": "10YGB----------A",
    "NO2": "10YNO-2--------T",
}

COUNTRIES = ["DE", "NL", "BE", "LU"]
GENERATION_COUNTRIES = ["DE", "NL", "BE"]

BORDERS = [
    ("DE", "NL"), ("DE", "BE"), ("DE", "FR"), ("DE", "AT"), ("DE", "CH"),
    ("DE", "PL"), ("DE", "CZ"), ("DE", "DK1"),
    ("NL", "BE"), ("NL", "GB"), ("NL", "NO2"),
    ("BE", "FR"), ("BE", "GB"),
]

COUNTRY_NAMES = {"DE": "Germany", "NL": "Netherlands", "BE": "Belgium", "LU": "Luxembourg"}

PSRTYPE_NAMES = {
    "B01": "Biomass", "B02": "Lignite", "B03": "Fossil coal-derived gas",
    "B04": "Natural gas", "B05": "Hard coal", "B06": "Fossil oil",
    "B07": "Fossil oil shale", "B08": "Fossil peat", "B09": "Geothermal",
    "B10": "Hydro pumped storage", "B11": "Hydro run-of-river",
    "B12": "Hydro water reservoir", "B13": "Marine", "B14": "Nuclear",
    "B15": "Other renewable", "B16": "Solar", "B17": "Waste",
    "B18": "Wind offshore", "B19": "Wind onshore", "B20": "Other",
    "B21": "AC link", "B22": "DC link", "B23": "Substation",
    "B24": "Transformer", "B25": "Energy storage",
}
RENEWABLE_PSR = {"B01", "B09", "B11", "B12", "B13", "B15", "B16", "B18", "B19"}
EXCLUDE_PSR = {"B10"}

SESSION = requests.Session()


def month_chunks(start: dt.date, end: dt.date):
    cur = dt.date(start.year, start.month, 1)
    while cur <= end:
        if cur.month == 12:
            nxt = dt.date(cur.year + 1, 1, 1)
        else:
            nxt = dt.date(cur.year, cur.month + 1, 1)
        chunk_start = max(cur, start)
        chunk_end = min(nxt, end + dt.timedelta(days=1))
        if chunk_start < chunk_end:
            yield chunk_start, chunk_end
        cur = nxt


def fmt(d: dt.date) -> str:
    return d.strftime("%Y%m%d0000")


def strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def local_tag(elem):
    return strip_ns(elem.tag)


def find(elem, tag):
    for child in elem:
        if local_tag(child) == tag:
            return child
    return None


def findall(elem, tag):
    return [c for c in elem if local_tag(c) == tag]


def call_api(params: dict, retries: int = 4):
    params = dict(params)
    params["securityToken"] = TOKEN
    for attempt in range(1, retries + 1):
        try:
            resp = SESSION.get(BASE_URL, params=params, timeout=60)
        except requests.RequestException as exc:
            print(f"  ! network error ({exc}), retry {attempt}/{retries}")
            time.sleep(3 * attempt)
            continue
        if resp.status_code == 200:
            return resp.text
        if resp.status_code == 429:
            print(f"  ! rate limited, waiting before retry {attempt}/{retries}")
            time.sleep(10 * attempt)
            continue
        if resp.status_code == 400 and b"No matching data" in resp.content:
            return None
        print(f"  ! HTTP {resp.status_code} for params={params} -- {resp.text[:200]}")
        return None
    return None


def parse_timeseries_points(xml_text: str):
    if not xml_text:
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    out = []
    for ts in findall(root, "TimeSeries"):
        psr_type = None
        mkt_psr = find(ts, "MktPSRType")
        if mkt_psr is not None:
            psr_el = find(mkt_psr, "psrType")
            if psr_el is not None:
                psr_type = psr_el.text

        biz_el = find(ts, "businessType")
        business_type = biz_el.text if biz_el is not None else None

        for period in findall(ts, "Period"):
            interval = find(period, "timeInterval")
            start_el = find(interval, "start") if interval is not None else None
            if start_el is None:
                continue
            start_dt = dt.datetime.strptime(start_el.text, "%Y-%m-%dT%H:%MZ").replace(tzinfo=dt.timezone.utc)

            res_el = find(period, "resolution")
            res_text = res_el.text if res_el is not None else "PT60M"
            if res_text == "PT15M":
                step_min = 15
            elif res_text == "PT30M":
                step_min = 30
            elif res_text in ("PT60M", "PT1H"):
                step_min = 60
            elif res_text == "P1D":
                step_min = 1440
            else:
                step_min = 60

            points = {}
            max_pos = 0
            for pt in findall(period, "Point"):
                pos_el = find(pt, "position")
                qty_el = find(pt, "quantity")
                if pos_el is None or qty_el is None:
                    continue
                pos = int(pos_el.text)
                try:
                    qty = float(qty_el.text)
                except ValueError:
                    continue
                points[pos] = qty
                if pos > max_pos:
                    max_pos = pos

            last = None
            for pos in range(1, max_pos + 1):
                if pos in points:
                    last = points[pos]
                if last is None:
                    continue
                ts_dt = start_dt + dt.timedelta(minutes=(pos - 1) * step_min)
                out.append({
                    "psrType": psr_type,
                    "businessType": business_type,
                    "ts": ts_dt,
                    "value": last,
                    "step_min": step_min,
                })
    return out


def daily_aggregate(points, group_keys=()):
    buckets = defaultdict(list)
    step_by_bucket = {}
    for p in points:
        date_str = p["ts"].astimezone(LOCAL_TZ).date().isoformat()
        key = (date_str,) + tuple(p.get(k) for k in group_keys)
        buckets[key].append(p["value"])
        step_by_bucket[key] = p["step_min"]

    result = {}
    for key, values in buckets.items():
        step_min = step_by_bucket[key]
        expected = max(1, round(1440 / step_min))
        result[key] = {
            "avg": sum(values) / len(values),
            "peak": max(values),
            "min": min(values),
            "count": len(values),
            "expected": expected,
        }
    return result


def fetch_load(code: str, start: dt.date, end: dt.date):
    all_points = []
    for cs, ce in month_chunks(start, end):
        print(f"  Load {code} {cs}..{ce}")
        xml_text = call_api({
            "documentType": "A65",
            "processType": "A16",
            "outBiddingZone_Domain": DOMAIN[code],
            "periodStart": fmt(cs),
            "periodEnd": fmt(ce),
        })
        all_points.extend(parse_timeseries_points(xml_text))
        time.sleep(0.3)
    return all_points


def fetch_generation(code: str, start: dt.date, end: dt.date):
    all_points = []
    for cs, ce in month_chunks(start, end):
        print(f"  Generation {code} {cs}..{ce}")
        xml_text = call_api({
            "documentType": "A75",
            "processType": "A16",
            "in_Domain": DOMAIN[code],
            "periodStart": fmt(cs),
            "periodEnd": fmt(ce),
        })
        all_points.extend(parse_timeseries_points(xml_text))
        time.sleep(0.3)
    return all_points


def fetch_flow_direction(in_code: str, out_code: str, start: dt.date, end: dt.date):
    all_points = []
    for cs, ce in month_chunks(start, end):
        xml_text = call_api({
            "documentType": "A11",
            "in_Domain": DOMAIN[in_code],
            "out_Domain": DOMAIN[out_code],
            "periodStart": fmt(cs),
            "periodEnd": fmt(ce),
        })
        all_points.extend(parse_timeseries_points(xml_text))
        time.sleep(0.3)
    return all_points


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    today = dt.datetime.now(dt.timezone.utc).date()

    load_daily = []
    for code in COUNTRIES:
        points = fetch_load(code, START_DATE, today)
        agg = daily_aggregate(points)
        for (date_str,), v in agg.items():
            if v["count"] < 0.8 * v["expected"]:
                continue
            load_daily.append({
                "code": code, "date": date_str,
                "avgMW": round(v["avg"], 2), "peakMW": round(v["peak"], 2),
                "minMW": round(v["min"], 2), "readings": v["count"],
            })
    load_daily.sort(key=lambda r: (r["code"], r["date"]))

    gen_daily = []
    for code in GENERATION_COUNTRIES:
        points = fetch_generation(code, START_DATE, today)
        points = [p for p in points if p["psrType"] not in EXCLUDE_PSR]
        agg = daily_aggregate(points, group_keys=("psrType",))
        for (date_str, psr), v in agg.items():
            if psr is None:
                continue
            if v["count"] < 0.5 * v["expected"]:
                continue
            gen_daily.append({
                "code": code, "date": date_str, "psrType": psr,
                "productionType": PSRTYPE_NAMES.get(psr, psr),
                "isRenewable": psr in RENEWABLE_PSR,
                "avgMW": round(v["avg"], 2),
            })
    gen_daily.sort(key=lambda r: (r["code"], r["date"], r["psrType"]))

    flow_daily = []
    for a, b in BORDERS:
        print(f"  Flows {a}<->{b}")
        pts_a_to_b = fetch_flow_direction(in_code=b, out_code=a, start=START_DATE, end=today)
        pts_b_to_a = fetch_flow_direction(in_code=a, out_code=b, start=START_DATE, end=today)
        agg_a_to_b = daily_aggregate(pts_a_to_b)
        agg_b_to_a = daily_aggregate(pts_b_to_a)
        dates = set(k[0] for k in agg_a_to_b) | set(k[0] for k in agg_b_to_a)
        for date_str in dates:
            fwd = agg_a_to_b.get((date_str,), {}).get("avg", 0.0)
            bwd = agg_b_to_a.get((date_str,), {}).get("avg", 0.0)
            flow_daily.append({
                "from": a, "to": b, "date": date_str,
                "netMW": round(fwd - bwd, 2),
            })
    flow_daily.sort(key=lambda r: (r["from"], r["to"], r["date"]))

    def latest_and_30d(code):
        rows = [r for r in load_daily if r["code"] == code]
        if not rows:
            return None
        rows.sort(key=lambda r: r["date"])
        latest = rows[-1]
        last30 = rows[-30:]
        avg30 = sum(r["avgMW"] for r in last30) / len(last30)
        latest_gw = latest["avgMW"] / 1000.0
        avg30_gw = avg30 / 1000.0
        pct = (latest_gw - avg30_gw) / avg30_gw if avg30_gw else 0.0
        return {
            "date": latest["date"], "latestGW": round(latest_gw, 2),
            "avg30dGW": round(avg30_gw, 2), "pct": round(pct, 4),
        }

    kpis = {code: latest_and_30d(code) for code in COUNTRIES}

    total_latest = sum(k["latestGW"] for k in kpis.values() if k)
    total_30d = sum(k["avg30dGW"] for k in kpis.values() if k)
    total_pct = (total_latest - total_30d) / total_30d if total_30d else 0.0
    latest_date = max((k["date"] for k in kpis.values() if k), default=None)
    direction = "above" if total_pct >= 0 else "below"
    headline = ""
    if latest_date:
        pretty_date = dt.datetime.strptime(latest_date, "%Y-%m-%d").strftime("%-d %B %Y")
        headline = (f"As of {pretty_date}, demand is running {abs(total_pct) * 100:.1f}% "
                    f"{direction} the 30-day average.")

    renewable_summary = {}
    for code in GENERATION_COUNTRIES:
        rows = [r for r in gen_daily if r["code"] == code]
        if not rows:
            continue
        latest_date_gen = max(r["date"] for r in rows)
        day_rows = [r for r in rows if r["date"] == latest_date_gen]
        total = sum(r["avgMW"] for r in day_rows)
        renewable = sum(r["avgMW"] for r in day_rows if r["isRenewable"])
        renewable_summary[code] = {
            "date": latest_date_gen,
            "totalGW": round(total / 1000.0, 2),
            "renewableGW": round(renewable / 1000.0, 2),
            "renewableShare": round(renewable / total, 4) if total else 0.0,
        }

    summary = {
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "headline": headline,
        "kpis": kpis,
        "renewable": renewable_summary,
        "countryNames": COUNTRY_NAMES,
    }

    with open(os.path.join(DATA_DIR, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    with open(os.path.join(DATA_DIR, "load_daily.json"), "w", encoding="utf-8") as f:
        json.dump(load_daily, f, indent=2)
    with open(os.path.join(DATA_DIR, "generation_daily.json"), "w", encoding="utf-8") as f:
        json.dump(gen_daily, f, indent=2)
    with open(os.path.join(DATA_DIR, "flows_daily.json"), "w", encoding="utf-8") as f:
        json.dump(flow_daily, f, indent=2)

    print(f"Done. Load rows={len(load_daily)}, Generation rows={len(gen_daily)}, Flow rows={len(flow_daily)}")
    print(f"Headline: {headline}")


if __name__ == "__main__":
    main()
