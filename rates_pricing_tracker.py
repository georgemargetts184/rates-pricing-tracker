"""Curve-implied policy path tracker: US, euro area, UK.

For each region, compares forward rates from the government/OIS curve with today's policy
rate, per forward bucket and over time. This is a PROXY for market pricing; true pricing
comes from futures/OIS (see futures_pricing.py).

  US   policy: EFFR (NY Fed)          curve: Treasury bill/note yields (3m, 6m, 1y, 2y)
  EA   policy: EUR short-term rate    curve: ECB AAA government spot curve
  UK   policy: Bank Rate (BoE)        curve: BoE SONIA OIS spot curve

US and EA use government yields as a proxy for OIS, so they carry a basis versus the
policy rate (bill-OIS, govt-OIS) that shows up as a small constant bias in the output.
The UK curve is OIS, so it is the cleanest of the three.
"""
import argparse
import io
import os
import subprocess
import time
import zipfile
from datetime import date, timedelta
from urllib.parse import urlencode

import matplotlib.pyplot as plt
import pandas as pd
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_PNG = os.path.join(HERE, "rates_pricing.png")
OUT_CSV = os.path.join(HERE, "rates_pricing_history.csv")

TENORS = [0.25, 0.5, 1.0, 2.0]                      # years
BUCKETS = ["0-3m", "3-6m", "6-12m", "12-24m"]       # forward windows between tenors
HIKE_BP = 25.0
START = date.today() - timedelta(days=365)

COLORS = {"US": "#2a78d6", "Euro area": "#eb6834", "UK": "#1baf7a"}


def get(url, **kw):
    r = requests.get(url, timeout=90, **kw)
    r.raise_for_status()
    return r


# ---- US -------------------------------------------------------------------
def us():
    frames = []
    for yr in range(START.year, date.today().year + 1):
        url = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
               f"daily-treasury-rates.csv/{yr}/all?type=daily_treasury_yield_curve"
               f"&field_tdr_date_value={yr}&page&_format=csv")
        frames.append(pd.read_csv(io.StringIO(get(url).text)))
    c = pd.concat(frames)
    c["Date"] = pd.to_datetime(c["Date"], format="%m/%d/%Y")
    curve = c.set_index("Date")[["3 Mo", "6 Mo", "1 Yr", "2 Yr"]].sort_index()
    curve.columns = TENORS

    j = get("https://markets.newyorkfed.org/api/rates/unsecured/effr/search.json",
            params={"startDate": START.isoformat(), "endDate": date.today().isoformat()}).json()
    pol = pd.Series({pd.Timestamp(r["effectiveDate"]): r["percentRate"] for r in j["refRates"]})
    return curve, pol.sort_index()


# ---- Euro area ------------------------------------------------------------
def ecb_series(path):
    df = pd.read_csv(io.StringIO(get(
        f"https://data-api.ecb.europa.eu/service/data/{path}",
        params={"startPeriod": START.isoformat(), "format": "csvdata"}).text),
        usecols=["TIME_PERIOD", "OBS_VALUE"])
    return pd.Series(df["OBS_VALUE"].values, index=pd.to_datetime(df["TIME_PERIOD"])).sort_index()


def euro():
    tenor_codes = {0.25: "SR_3M", 0.5: "SR_6M", 1.0: "SR_1Y", 2.0: "SR_2Y"}
    curve = pd.DataFrame({t: ecb_series(f"YC/B.U2.EUR.4F.G_N_A.SV_C_YM.{c}")
                          for t, c in tenor_codes.items()})
    return curve, ecb_series("EST/B.EU000A2X2A25.WT")


# ---- UK -------------------------------------------------------------------
def boe_ois_sheet(book, sheet):
    raw = book.parse(sheet, header=None)
    yrow = raw.index[raw[0].astype(str).str.strip().str.lower() == "years:"][0]
    years = pd.to_numeric(raw.iloc[yrow, 1:], errors="coerce")
    data = raw.iloc[yrow + 1:].copy()
    data[0] = pd.to_datetime(data[0], errors="coerce")
    data = data.dropna(subset=[0]).set_index(0)
    out = {}
    for t in TENORS:
        hit = years.index[(years - t).abs() < 1e-6]
        if len(hit):
            out[t] = pd.to_numeric(data[hit[0]], errors="coerce")
    return pd.DataFrame(out)


def curl_get(url, expect, tries=5, **params):
    """Fetch via the system curl (the BoE site returns 403 to python-requests).

    The BoE endpoints occasionally return an HTML error page with a 200, so a response
    that does not start with `expect` is retried after a short pause."""
    if params:
        url += "?" + urlencode(params, safe="/")
    err = "no attempts"
    for attempt in range(tries):
        if attempt:
            time.sleep(3 * attempt)
        res = subprocess.run(["curl", "-sSL", "--fail", "-m", "120", url], capture_output=True)
        if res.returncode:
            err = f"curl exit {res.returncode}: {res.stderr.decode().strip()}"
        elif res.stdout.startswith(expect):
            return res.stdout
        else:
            err = f"unexpected response starting {res.stdout[:40]!r}"
    raise RuntimeError(f"{err} for {url} after {tries} tries")


def uk():
    z = zipfile.ZipFile(io.BytesIO(curl_get(
        "https://www.bankofengland.co.uk/-/media/boe/files/statistics/yield-curves/oisddata.zip",
        expect=b"PK")))
    name = next(n for n in z.namelist() if "2025 to present" in n)
    book = pd.ExcelFile(io.BytesIO(z.read(name)))
    # monthly maturities to 60m, so it covers 3m, 6m, 1y and 2y on its own
    curve = boe_ois_sheet(book, "3. spot, short end").dropna().sort_index()

    d0, d1 = START.strftime("%d/%b/%Y"), date.today().strftime("%d/%b/%Y")
    txt = curl_get("https://www.bankofengland.co.uk/boeapps/iadb/fromshowcolumns.asp",
                   expect=b"DATE", **{"csv.x": "yes", "Datefrom": d0, "Dateto": d1, "SeriesCodes": "IUDBEDR",
                      "CSVF": "TN", "UsingCodes": "Y"}).decode()
    br = pd.read_csv(io.StringIO(txt))
    pol = pd.Series(br["IUDBEDR"].values, index=pd.to_datetime(br["DATE"], format="%d %b %Y"))
    return curve, pol.sort_index()


# ---- maths ----------------------------------------------------------------
def pricing(region, curve, policy, basis_bp=0.0):
    """Forward rate in each bucket minus the policy rate, in bp (spot yields, simple maths).

    basis_bp is subtracted from every bucket: the constant gap between this government
    curve and the policy rate that is not a rate expectation (bill-OIS / funding basis).
    It does NOT remove term premium, which grows with horizon."""
    d = curve.join(policy.rename("policy"), how="inner").dropna()
    d = d[d.index >= pd.Timestamp(START)]
    ts = [0.0] + TENORS
    out = pd.DataFrame(index=d.index)
    out["policy"] = d["policy"]
    for i, name in enumerate(BUCKETS):
        t1, t2 = ts[i], ts[i + 1]
        y1 = d[t1] if t1 else 0.0
        fwd = (d[t2] * t2 - y1 * t1) / (t2 - t1)
        out[name] = (fwd - d["policy"]) * 100.0 - basis_bp
    out["avg_12m"] = (d[1.0] - d["policy"]) * 100.0 - basis_bp   # avg implied change, next 12m
    out.insert(0, "region", region)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--basis-us", type=float, default=0.0, help="US bill-vs-EFFR basis to remove, bp")
    ap.add_argument("--basis-ea", type=float, default=0.0, help="EA govt-vs-EUR short-term rate basis, bp")
    ap.add_argument("--long", action="store_true", help="also chart the 12-24m window (mostly term premium)")
    args = ap.parse_args()
    basis = {"US": args.basis_us, "Euro area": args.basis_ea, "UK": 0.0}
    shown = BUCKETS if args.long else BUCKETS[:3]

    frames = []
    for region, fn in (("US", us), ("Euro area", euro), ("UK", uk)):
        try:
            curve, policy = fn()
        except requests.HTTPError as e:
            print(f"{region}: skipped ({e.response.status_code} from {e.response.url})")
            continue
        except RuntimeError as e:          # curl_get failure (BoE)
            print(f"{region}: skipped ({e})")
            continue
        p = pricing(region, curve, policy, basis[region])
        print(f"{region}: {len(p)} days, {p.index.min().date()} -> {p.index.max().date()}")
        frames.append(p)
    hist = pd.concat(frames)
    hist.index.name = "date"
    hist.round(2).to_csv(OUT_CSV)

    latest = hist.groupby("region").tail(1).set_index("region")
    print("\nCurve-implied path (bp vs policy rate, after basis adj; 25bp = one hike).")
    print("US/EA: government-yield proxy, NOT futures/OIS; UK: SONIA OIS. 12-24m is mostly term premium.")
    for r in latest.index:
        print(f"  {r} as of {hist[hist.region == r].index.max().date()}")
    print(latest[["policy"] + BUCKETS + ["avg_12m"]].round(1).to_string())
    print("\nCurve-implied hikes, avg over next 12m (avg_12m / 25bp):")
    print((latest["avg_12m"] / HIKE_BP).round(2).to_string())

    plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False})
    fig, (a, b) = plt.subplots(1, 2, figsize=(13, 5), gridspec_kw={"width_ratios": [1, 1.4]})

    for region, row in latest.iterrows():
        a.plot(shown, row[shown].values, marker="o", ms=7, lw=2, color=COLORS[region],
               label=region)
        a.annotate(region, (len(shown) - 1, row[shown].values[-1]), xytext=(8, 0),
                   textcoords="offset points", va="center", color="#52514e")
    a.axhline(0, color="#999", lw=0.8)
    a.set_title("Curve-implied path: forward rate vs policy rate", loc="left", fontsize=11)
    a.set_ylabel("bp (25bp = one hike)")
    a.set_xlabel("Forward window")
    a.margins(x=0.25)
    a.legend(frameon=False, loc="upper left")

    for region, g in hist.groupby("region"):
        b.plot(g.index, g["avg_12m"], lw=2, color=COLORS[region], label=region)
    b.axhline(0, color="#999", lw=0.8)
    b.set_title("Curve-implied average policy-rate change, next 12 months", loc="left", fontsize=11)
    b.set_ylabel("bp (25bp = one hike)")
    b.legend(frameon=False, loc="upper left")
    for ax in (a, b):
        ax.grid(axis="y", alpha=0.25)
        lo, hi = ax.get_ylim()
        ax.set_yticks([x for x in range(int(lo // 25) * 25, int(hi // 25 + 2) * 25, 25)
                       if lo <= x <= hi])

    regions = list(latest.index)
    asof = ", ".join(f"{r} {latest.loc[r].name and hist[hist.region == r].index.max():%d %b}"
                     for r in latest.index)
    note = (f"US/EA: government-yield proxy (basis removed bp: US {basis['US']:.0f}, "
            f"EA {basis['Euro area']:.0f}; term premium NOT removed). UK: SONIA OIS. As of: {asof}.")
    fig.suptitle("Curve-implied policy path: " + ", ".join(regions), x=0.01, ha="left", fontsize=13)
    fig.text(0.01, 0.01, note + " Not trading advice.", fontsize=8, color="#52514e")
    fig.tight_layout(rect=(0, 0.03, 1, 0.95))
    fig.savefig(OUT_PNG, dpi=150)
    print("\nSaved", OUT_PNG, "and", OUT_CSV)


if __name__ == "__main__":
    main()
