"""Meeting-by-meeting implied policy moves from 30-day fed funds futures (ZQ).

Input CSV columns: meeting_date, contract_price
  - meeting_date: date of the policy decision (the new rate applies from the next day)
  - contract_price: price of the ZQ contract for the calendar month containing that meeting
Rows must be consecutive meetings, each with its own month's contract.

A ZQ contract settles at 100 minus the AVERAGE effective fed funds rate for its month, so for
a meeting on day d of an N-day month:
    F = (rate_before * d + rate_after * (N - d)) / N        where F = 100 - price
    rate_after = (F * N - rate_before * d) / (N - d)
Each meeting's rate_before is the previous meeting's implied rate_after (starting from EFFR).
"""
import argparse
import calendar
import sys

import pandas as pd
import requests


def current_effr():
    j = requests.get("https://markets.newyorkfed.org/api/rates/unsecured/effr/last/1.json",
                     timeout=30).json()
    return float(j["refRates"][0]["percentRate"])


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("input", help="CSV with meeting_date, contract_price")
    ap.add_argument("--current", type=float, help="current EFFR in %% (default: fetch from NY Fed)")
    ap.add_argument("--step", type=float, default=25.0, help="standard move size in bp")
    args = ap.parse_args()

    if "EXAMPLE" in args.input.upper():
        print("*** EXAMPLE INPUT: made-up prices, not market data ***\n")

    df = pd.read_csv(args.input, comment="#", parse_dates=["meeting_date"]).sort_values("meeting_date")
    start = args.current if args.current is not None else current_effr()
    rate, rows = start, []
    for _, r in df.iterrows():
        d = r["meeting_date"]
        n = calendar.monthrange(d.year, d.month)[1]
        days_old = d.day                       # old rate applies through the meeting date
        f = 100.0 - r["contract_price"]
        after = (f * n - rate * days_old) / (n - days_old)
        move = (after - rate) * 100.0
        flag = "  <- <7 days left in month: noisy, use next month's contract" if n - days_old < 7 else ""
        rows.append({"meeting": d.date(), "rate_before": round(rate, 4), "rate_after": round(after, 4),
                     "implied_move_bp": round(move, 1), "pct_of_step": round(100 * move / args.step),
                     "cum_bp": round((after - start) * 100.0, 1), "note": flag.strip()})
        rate = after

    out = pd.DataFrame(rows)
    print(f"Starting rate (EFFR): {start:.2f}%   step: {args.step:.0f}bp")
    print(out.to_string(index=False))


if __name__ == "__main__":
    sys.exit(main())
