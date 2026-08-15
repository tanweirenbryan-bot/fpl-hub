# fpl-hub — Layer 0

Snapshot store for Fantasy Premier League 2026/27. Runs twice daily, commits
what it finds, and verifies it.

**This repo does not model anything.** It exists to accumulate history, because
prices and ownership as they *were* cannot be reconstructed after the fact —
and backtesting is impossible without them. Every day this is not running is a
day of history permanently lost.

---

## Setup (about 15 minutes)

### 1. Create the repository

Make a **public** repo on GitHub named `fpl-hub`. Public matters: private repos
get a limited monthly allowance of Actions minutes; public repos get unlimited.

### 2. Push these files

```bash
git init
git add .
git commit -m "Layer 0: snapshot pipeline"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/fpl-hub.git
git push -u origin main
```

### 3. Run it once by hand

Go to the **Actions** tab → **FPL snapshot** → **Run workflow**.

Do this immediately rather than waiting for the schedule. If something is
misconfigured you want to find out now, not at 02:17 tomorrow.

A green tick and a new commit named `snapshot: ...` means it works.

### 4. Confirm the schedule is live

After the manual run, the Actions tab should show the next scheduled run.
Nothing further to configure.

---

## Running locally

```bash
pip install -r requirements.txt

python snapshot.py                  # pull from the official FPL API
python snapshot.py --source mirror  # pull from the CSV mirror instead
python snapshot.py --dry-run        # fetch and check, write nothing

python verify.py                    # run the gates
python verify.py --strict           # treat warnings as failures too
```

---

## What gets written

```
data/
├── snapshots/
│   ├── players/date=2026-08-15/players_20260815T0217Z.parquet
│   ├── teams/date=2026-08-15/teams_20260815T0217Z.parquet
│   ├── fixtures/date=2026-08-15/fixtures_20260815T0217Z.parquet
│   └── events/date=2026-08-15/events_20260815T0217Z.parquet
└── latest/
    ├── players.csv     ← human-readable, overwritten each run
    ├── teams.csv
    └── ...
```

Parquet for the archive because twice-daily CSVs of 587 players × ~100 columns
would bloat the repo; Parquet is columnar and compressed. CSV for `latest/` so
you can eyeball the current data in the GitHub web UI without downloading
anything.

**Expected growth: roughly 10–15 MB per season.** Well within any limit.

### Reading the history back

```python
import pandas as pd

# One day
df = pd.read_parquet("data/snapshots/players/date=2026-08-15/")

# The whole season — every snapshot carries snapshot_utc, so this is a
# usable time series of prices and ownership out of the box.
df = pd.read_parquet("data/snapshots/players/")
prices = df.pivot_table(index="snapshot_utc", columns="web_name",
                        values="price_gbp_m")
```

---

## Data sources

| Source | Used for | Notes |
|---|---|---|
| [FPL API](https://fantasy.premierleague.com/api/bootstrap-static/) | Primary | Unofficial and undocumented. No published rate limit; we send a real User-Agent and pull twice a day |
| [FPL-Core-Insights](https://github.com/olbauday/FPL-Core-Insights) | Fallback | Refreshed twice daily. **Licence is only an attribution request**, not a formal open-source licence — treat as unresolved if you ever publish from it |

### The units trap

`now_cost` means different things in the two sources:

| Source | Haaland appears as | Meaning |
|---|---|---|
| FPL API | `155` | tenths of a million |
| CSV mirror | `15.5` | already millions |

`snapshot.py` normalises both to a `price_gbp_m` column and **hard-fails if the
maximum price falls outside £4m–£20m**. This bug was hit for real during
development: the code ran clean and produced a £1.55m Haaland. It raises no
exception, so only a range check catches it.

---

## Verification gates

`verify.py` runs after every snapshot. Each gate exists because that failure
mode produces *plausible wrong output* rather than an error.

| Gate | Catches |
|---|---|
| `player_count` | Truncated download |
| `price_units` | The 10× bug above |
| `positions` | A new position type being added mid-season |
| `numeric:expected_*` | xG served as strings — `"9.5" > "10.2"` in string order |
| `empty_columns` | Columns that exist but are 100% null (7 of them currently, including `set_piece_threat`). **Never use these as model features** |
| `unique_player_id` | Duplicate ids breaking every downstream join |
| `team_count` | Must be exactly 20 |
| `player_team_join` | Players silently vanishing in an inner join |
| `fixtures_per_team_gw` | Duplicated fixture rows (>2 per team per GW is impossible) |
| `snapshot_history` | The archive actually accumulating |

---

## Known limitations

- **The API path has not been tested end-to-end.** It was written against the
  documented response shape and the schema check will fail loudly if that shape
  is wrong, but the first real Actions run is the first true test. Watch it.
- **Scheduled workflows are disabled after 60 days of repo inactivity.** GitHub
  emails you first. Push any commit to re-enable.
- **Scheduled runs are not punctual.** GitHub queues them; delays of 10–30
  minutes are normal. Never schedule a pull for just before a deadline.
- **The mirror fallback has a different schema** to the API, including no
  fixtures file. Anything downstream must not assume the two are identical.

---

## What comes next

This is Layer 0 of a four-layer design. Nothing here models anything, by design.

| Layer | What | When |
|---|---|---|
| **0. Data** | this repo | now |
| 1. Components | minutes · team strength · attack rate · DefCon · bonus | GW1–12 |
| 2. Composition | match-level Monte Carlo | GW13+ |
| 3. Query | rank · haul probability · mispricing · solver input | GW15+ |
