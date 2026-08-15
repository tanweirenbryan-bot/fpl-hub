#!/usr/bin/env python3
"""
Verification gates for the Layer 0 snapshot.

WHY THIS EXISTS
---------------
Data bugs in this domain do not raise exceptions. They produce plausible
output that is wrong. Two real examples from building this project:

  * Dividing `now_cost` by 10 on a source that had already done so. Result:
    a maximum forward price of £1.55m. The code ran clean.
  * Treating xG columns as numbers when the API serves them as strings. In
    string ordering "9.5" > "10.2", so the top-scorer table silently inverts.

Every gate below exists because something like that can happen without an
error being raised. Run it after every snapshot. A red build is cheap; a
model trained on corrupt history is not.

Exit code 0 = all gates passed. 1 = at least one FAIL.
WARN does not fail the build but is printed for you to read.

USAGE
-----
    python verify.py                 # check data/latest/
    python verify.py --strict        # treat warnings as failures too
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent
LATEST = ROOT / "data" / "latest"
SNAP = ROOT / "data" / "snapshots"

# Collected results: (level, gate name, message)
RESULTS = []


def gate(name, condition, msg, level="FAIL"):
    """Record one check. `condition` True means the gate passed."""
    RESULTS.append(("PASS" if condition else level, name, msg))
    return condition


# --------------------------------------------------------------------------
# Gates
# --------------------------------------------------------------------------

def check_players(df):
    n = len(df)
    # 2026/27 has 587 players at season start; squads change with transfers and
    # injuries, so a band rather than an exact number. Outside this band means
    # a truncated download, not a busy transfer window.
    gate("player_count", 400 <= n <= 900,
         f"{n} players (expect 400-900; truncated download if far below)")

    # Price. The 10x bug lands here. FPL prices have never left £3.5m-£20m.
    if "price_gbp_m" in df.columns:
        lo, hi = df["price_gbp_m"].min(), df["price_gbp_m"].max()
        gate("price_units", 3.5 <= lo and 4.0 <= hi <= 20.0,
             f"price range £{lo}m - £{hi}m (a max near £1.5m means units are 10x wrong)")

    # Positions. element_type 1-4 = GKP/DEF/MID/FWD. A fifth value means the
    # game added a position (managers were added as a chip-like element once).
    pos_col = "element_type" if "element_type" in df.columns else "position"
    if pos_col in df.columns:
        k = df[pos_col].nunique()
        gate("positions", k == 4, f"{k} distinct positions (expect 4)")

    # xG columns must be castable to numbers WITHOUT losing values.
    #
    # NOTE: an earlier version of this gate called
    #     pd.api.types.is_numeric_dtype(pd.to_numeric(col, errors="coerce"))
    # which always passes -- to_numeric with coerce ALWAYS returns a numeric
    # dtype, turning anything unparseable into NaN. The gate tested nothing.
    # The real question is whether coercion silently destroys data, so we
    # compare non-null counts before and after.
    for c in ["expected_goals", "expected_assists", "expected_goal_involvements"]:
        if c in df.columns:
            before = df[c].notna().sum()
            after = pd.to_numeric(df[c], errors="coerce").notna().sum()
            gate(f"numeric:{c}", before == after,
                 f"{before - after} of {before} values unparseable as numbers")

    # Fully-empty columns. These are traps: a feature that trains fine and
    # contributes nothing, with no warning. In the 2026/27 mirror, seven columns
    # are 100% null including set_piece_threat.
    empty = [c for c in df.columns if df[c].notna().sum() == 0]
    if empty:
        gate("empty_columns", False,
             f"{len(empty)} fully-null columns -> do NOT use as features: {empty}",
             level="WARN")
    else:
        gate("empty_columns", True, "none")

    # Duplicate player ids would break every join downstream.
    idc = "id" if "id" in df.columns else "player_id"
    if idc in df.columns:
        d = df[idc].duplicated().sum()
        gate("unique_player_id", d == 0, f"{d} duplicate ids")


def check_teams(df):
    gate("team_count", len(df) == 20, f"{len(df)} teams (must be 20)")
    if "name" in df.columns:
        gate("team_names_unique", df["name"].nunique() == len(df),
             f"{df['name'].nunique()} distinct names")


def check_join(players, teams):
    """Every player must resolve to a team. Unmatched rows vanish in an inner
    join and shrink your dataset silently -- the classic invisible data loss."""
    left = "team" if "team" in players.columns else "team_code"
    right = "id" if left == "team" else "code"
    if left not in players.columns or right not in teams.columns:
        gate("player_team_join", True, "join columns not present in this source", level="WARN")
        return
    merged = players.merge(teams[[right]], left_on=left, right_on=right, how="left")
    unmatched = merged[right].isna().sum()
    gate("player_team_join", unmatched == 0,
         f"{unmatched} players did not match a team")


def check_fixtures(df):
    if df is None or df.empty:
        gate("fixtures_present", True, "no fixtures file in this source", level="WARN")
        return
    # A full Premier League season is 380 fixtures. Fewer means a partial pull.
    gate("fixture_count", 300 <= len(df) <= 400, f"{len(df)} fixtures (expect 380)")
    if {"team_h", "team_a", "event"} <= set(df.columns):
        # Each team plays at most once per gameweek, except in double gameweeks.
        # More than 2 is impossible and means duplicated rows.
        counts = pd.concat([
            df.groupby(["event", "team_h"]).size(),
            df.groupby(["event", "team_a"]).size(),
        ])
        gate("fixtures_per_team_gw", counts.max() <= 2,
             f"max fixtures for one team in one GW = {counts.max()} (>2 is impossible)")


def check_history():
    """Snapshots are only valuable as a time series. Confirm one is accumulating."""
    files = sorted(SNAP.glob("players/date=*/players_*.parquet"))
    gate("snapshot_history", len(files) >= 1, f"{len(files)} player snapshots on disk")
    if len(files) >= 2:
        stamps = [f.stem.split("_")[-1] for f in files]
        gate("snapshots_unique", len(set(stamps)) == len(stamps),
             f"{len(set(stamps))} distinct timestamps across {len(stamps)} files")


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true", help="treat WARN as failure")
    args = ap.parse_args()

    if not (LATEST / "players.csv").exists():
        print("FATAL: data/latest/players.csv not found -- run snapshot.py first",
              file=sys.stderr)
        return 1

    players = pd.read_csv(LATEST / "players.csv")
    teams = pd.read_csv(LATEST / "teams.csv") if (LATEST / "teams.csv").exists() else pd.DataFrame()
    fixtures = pd.read_csv(LATEST / "fixtures.csv") if (LATEST / "fixtures.csv").exists() else None

    check_players(players)
    if not teams.empty:
        check_teams(teams)
        check_join(players, teams)
    check_fixtures(fixtures)
    check_history()

    # ---- report -------------------------------------------------------
    width = max(len(n) for _, n, _ in RESULTS) + 2
    print("\nVERIFICATION GATES")
    print("-" * 78)
    for level, name, msg in RESULTS:
        mark = {"PASS": "ok  ", "WARN": "warn", "FAIL": "FAIL"}[level]
        print(f"  [{mark}] {name:<{width}} {msg}")
    print("-" * 78)

    fails = sum(1 for l, _, _ in RESULTS if l == "FAIL")
    warns = sum(1 for l, _, _ in RESULTS if l == "WARN")
    print(f"  {len(RESULTS) - fails - warns} passed, {warns} warnings, {fails} failures")

    if fails or (args.strict and warns):
        print("\nGATES FAILED -- do not trust this snapshot.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
