#!/usr/bin/env python3
"""
Layer 0: snapshot the current state of FPL to disk.

WHY THIS EXISTS
---------------
Prices, ownership and player status change continuously and the FPL API only
ever serves you *now*. Backtesting requires knowing prices as they WERE at the
moment a decision was available. A snapshot you fail to take today cannot be
reconstructed later at any price. That is the entire justification for running
this twice a day from day one, long before any model exists.

WHAT IT WRITES
--------------
    data/snapshots/players/date=YYYY-MM-DD/players_YYYYMMDDTHHMMZ.parquet
    data/snapshots/teams/date=YYYY-MM-DD/teams_YYYYMMDDTHHMMZ.parquet
    data/snapshots/fixtures/date=YYYY-MM-DD/fixtures_YYYYMMDDTHHMMZ.parquet
    data/snapshots/events/date=YYYY-MM-DD/events_YYYYMMDDTHHMMZ.parquet
    data/latest/*.csv        <- human-readable copy of the most recent pull

Parquet because twice-daily CSVs of ~600 players x ~100 columns bloats a repo
fast; Parquet is columnar and compressed, typically 5-10x smaller.

USAGE
-----
    python snapshot.py                 # pull from the official FPL API
    python snapshot.py --source mirror # pull from the FPL-Core-Insights CSVs
    python snapshot.py --dry-run       # fetch and validate, write nothing
"""

import argparse                      # parse command-line flags
import io                            # wrap downloaded bytes so pandas can read them
import json                          # decode the API response
import sys                           # exit codes: 0 = success, 1 = failure
import time                          # sleep between retries
from datetime import datetime, timezone   # UTC timestamps; never use local time
from pathlib import Path             # filesystem paths that work on Win/Mac/Linux

import pandas as pd                  # dataframes
import requests                      # HTTP client

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

API = "https://fantasy.premierleague.com/api"

# Fallback source. Refreshed twice daily, already type-cast, and reachable from
# networks that block the FPL API. Note its licensing is only an attribution
# request, not a formal open-source licence.
MIRROR = ("https://raw.githubusercontent.com/olbauday/"
          "FPL-Core-Insights/main/data/2026-2027")

# Identify ourselves. The FPL API is undocumented and unofficial; sending a
# real User-Agent is basic courtesy and makes us easier to rate-limit politely
# rather than block outright.
HEADERS = {"User-Agent": "fpl-hub-snapshot/1.0 (personal research project)"}

ROOT = Path(__file__).parent
SNAP = ROOT / "data" / "snapshots"
LATEST = ROOT / "data" / "latest"

# Columns we require to exist. If the API silently renames or drops one of
# these, we want a loud failure now, not a wrong model in November.
REQUIRED_PLAYER_COLS = [
    "id", "web_name", "team", "element_type", "now_cost", "total_points",
    "minutes", "selected_by_percent", "expected_goals", "expected_assists",
    "expected_goal_involvements", "expected_goals_conceded",
]


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def get(url, retries=3, backoff=5):
    """GET with retries.

    Scheduled jobs fail for boring reasons: transient DNS, a blip at the far
    end, GitHub's runner having a bad minute. Three tries with a widening gap
    turns most of those into a non-event instead of a lost snapshot.
    """
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()          # turn 4xx/5xx into an exception
            return r
        except Exception as exc:
            if attempt == retries:
                raise                      # out of tries: let it fail loudly
            wait = backoff * attempt       # 5s, then 10s
            print(f"  attempt {attempt}/{retries} failed ({exc}); retrying in {wait}s",
                  file=sys.stderr)
            time.sleep(wait)


def fetch_api():
    """Pull the four endpoints we care about from the official API."""
    print("source: official FPL API")

    boot = json.loads(get(f"{API}/bootstrap-static/").text)
    players = pd.DataFrame(boot["elements"])
    teams = pd.DataFrame(boot["teams"])
    events = pd.DataFrame(boot["events"])
    fixtures = pd.DataFrame(json.loads(get(f"{API}/fixtures/").text))

    # UNITS. The API stores price in tenths of a million: 155 means £15.5m.
    # This is the single most common silent bug in FPL analysis -- it produces
    # plausible-looking output that is wrong by exactly 10x and raises no error.
    # We normalise once, here, and record what we did.
    players["price_gbp_m"] = players["now_cost"] / 10.0
    players.attrs["price_source"] = "api_tenths"

    return players, teams, events, fixtures


def fetch_mirror():
    """Pull from the FPL-Core-Insights CSV mirror.

    Used when the API is unreachable, and for local testing. The schema differs
    from the API's, so anything downstream must not assume they are identical.
    """
    print("source: FPL-Core-Insights CSV mirror")

    def csv(name):
        return pd.read_csv(io.StringIO(get(f"{MIRROR}/{name}").text))

    meta = csv("players.csv")          # names, positions, team_code
    stats = csv("playerstats.csv")     # prices, xG, ownership
    teams = csv("teams.csv")           # includes ClubElo, pre-joined
    events = csv("gameweek_summaries.csv")

    players = stats.merge(meta, left_on="id", right_on="player_id", how="left")

    # UNITS AGAIN, AND DIFFERENTLY. This mirror has ALREADY divided by 10:
    # Haaland appears as 15.5, not 155. Applying the API's /10 here would give
    # a £1.55m Haaland. I made exactly this mistake on this dataset -- the code
    # ran clean and the answer was nonsense. Hence the guard below.
    players["price_gbp_m"] = players["now_cost"].astype(float)
    players.attrs["price_source"] = "mirror_already_millions"

    return players, teams, events, pd.DataFrame()   # mirror has no fixtures file


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------

def write(frames, stamp, source, dry_run=False):
    """Write each frame to a date-partitioned Parquet file, plus a latest CSV.

    Partitioning by date keeps directory listings small and lets you read a
    single day cheaply later, e.g. with pandas' `filters=` argument.
    """
    day = stamp[:8]                                   # YYYYMMDD
    day_fmt = f"{day[:4]}-{day[4:6]}-{day[6:]}"       # YYYY-MM-DD

    written = []
    for name, df in frames.items():
        if df is None or df.empty:
            print(f"  skip {name}: empty")
            continue

        # Stamp every row so a snapshot is self-describing once it is merged
        # with others. Without this, concatenated snapshots are unusable.
        df = df.copy()
        df["snapshot_utc"] = stamp
        df["snapshot_source"] = source

        out_dir = SNAP / name / f"date={day_fmt}"
        out_path = out_dir / f"{name}_{stamp}.parquet"

        if not dry_run:
            out_dir.mkdir(parents=True, exist_ok=True)
            df.to_parquet(out_path, index=False, compression="snappy")

            # A human-readable copy of the newest pull. Overwritten each run, so
            # it costs no repo growth, and it means you can eyeball the data on
            # GitHub without downloading Parquet.
            LATEST.mkdir(parents=True, exist_ok=True)
            df.to_csv(LATEST / f"{name}.csv", index=False)

        size = out_path.stat().st_size / 1024 if (not dry_run and out_path.exists()) else 0
        print(f"  {name:9s} {len(df):5d} rows x {len(df.columns):3d} cols  ({size:6.1f} KB)")
        written.append(out_path)

    return written


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["api", "mirror"], default="api",
                    help="where to pull from (default: api)")
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch and validate but write nothing")
    args = ap.parse_args()

    # UTC, always. GitHub Actions runners are UTC; your laptop is not. Mixing
    # the two produces snapshots that appear to travel backwards in time.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%MZ")
    print(f"FPL snapshot {stamp}")

    players, teams, events, fixtures = (
        fetch_api() if args.source == "api" else fetch_mirror()
    )

    # --- fail fast on schema drift -------------------------------------
    # Cheap check, run before writing. If the API changes shape we want to know
    # from a red build today, not from a broken model in three months.
    if args.source == "api":
        missing = [c for c in REQUIRED_PLAYER_COLS if c not in players.columns]
        if missing:
            print(f"FATAL: expected columns missing from API: {missing}", file=sys.stderr)
            return 1

    # --- the 10x guard --------------------------------------------------
    # Whatever the source claimed, the most expensive player in FPL has always
    # been between £4m and £20m. If we are outside that, units are wrong.
    top = players["price_gbp_m"].max()
    if not (4.0 <= top <= 20.0):
        print(f"FATAL: max price = {top} -- price units are wrong "
              f"(source said '{players.attrs.get('price_source')}')", file=sys.stderr)
        return 1
    print(f"  price check ok: max = £{top}m")

    frames = {"players": players, "teams": teams,
              "events": events, "fixtures": fixtures}
    write(frames, stamp, args.source, dry_run=args.dry_run)

    if args.dry_run:
        print("dry run: nothing written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
