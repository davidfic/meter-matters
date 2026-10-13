# How Meter Matters works

> The long-form reference. The [README](../README.md) is the short version,
> and the app's own `/docs` pages are the user-facing setup guide with screenshots;
> this is where the reasoning lives: how the weather is taken out, how a step
> change is found, how bills pair with metered periods, what each way of getting
> bills in can and cannot see, and the single-machine tooling (IMAP polling,
> the systemd timer) that predates the hosted app. Some sections still describe
> Eversource specifically; the app itself now takes any utility's files.

A web app for seeing what your gas and electricity actually did, and using
less of both. It reads your utility's exports and shows:

- **How you compare with the same weeks last year** - with the weather taken
  out, so a cold winter isn't mistaken for a wasteful one.
- **When something changed** - usage that stepped to a new level and stayed.
- **Your carbon footprint**, and what specific changes would be worth in your
  own home.

On the electric pages it still answers to ⚡ *Watt's the Damage*; on the gas ones, 🔥 *Therms & Conditions*.

Behind that is the full history of every bill, and a game - streaks, levels,
badges - for anyone who wants one. Nothing in it is awarded for showing up: if
a badge is lit, your bills earned it.

## Run it

```bash
./run.sh              # creates .venv on first run, then serves on :5000
PORT=5137 ./run.sh    # pick a different port
```

Then open <http://127.0.0.1:5000>.

That is the development server, on localhost only. To put it on the internet
see [Hosting it](#hosting-it).

## Pages

| Route | What it is |
|---|---|
| `/` | Combined overview - both fuels side by side, alerts, carbon footprint, and total spend |
| `/electric` | Everything about electricity: against last year, what changed, history, the game |
| `/gas` | The same for gas |
| `/raw` | Every record, filterable and exportable |
| `/rates` | Your price per kWh taken apart, and the supply offers around you |
| `/homes` | Every home added up - only once there is more than one |
| `/home/<name>/...` | Any of the above, for a second home |

The overview lifts its headline numbers out of each fuel's own report rather
than computing a third version, so it can never disagree with the page it links
to. Streaks, levels and badges stay **per fuel** - a combined game would hide
which half you actually moved, and gas is near-zero for half the year.

## The test report

`/test-suite` shows the results of the last test run. It is open to anyone and
not linked from anywhere - there is nothing private in it, since the tests run
on invented data - and **visiting it runs nothing**. The suite never runs on
the server (it patches the environment and the network while it runs, and the
image doesn't carry the tests). Instead `./deploy/cloudrun.sh deploy` runs it on
your machine first, against a throwaway Postgres in Docker (so the database tests run
too - never against the real Supabase, which they would empty) and writes the results to `test-report.json`, which ships
with the code it describes - so the page always reports on exactly what is
running. A failing suite is still deployed; the page says so.

Run it yourself any time with `python -m metermatters.testreport` (writes `test-report.json`
and prints a summary). The page shows the verdict, counts, every test by file
with its time, and what failed with the traceback first.

## Any utility, any state

Nothing in the analysis depends on who the utility is. What gets bills and usage
in is general, and the few things that depend on place are asked about it.

**Getting data in** (Add bills page; the dashboard's drop zone takes the same files):

| You have | What happens |
|---|---|
| A usage file, with costs | Read, added up into months if it is daily or hourly, and filed. If the home has no bill history, a bill is made from each period (marked as an estimate, because the due date is guessed), so the dashboard has something to show |
| A bill history | Each row becomes a statement |
| Green Button XML | Interval readings added up by month. Units are read from the file's own unit code, and one that isn't recognised is refused rather than guessed |
| Any other spreadsheet | Columns are found by name. If it can't tell which is which, or what unit it is in, it shows a sample and asks - it never guesses a unit |
| Nothing to upload | Type a bill in, with the usage if you have it |

Eversource's own layouts are still read first, exactly as before.

**Where a home is** (State, ZIP and utility on the Add bills page, all optional;
the state is otherwise read off the address in a usage file):

- *Carbon* uses the grid for the home's state: EPA eGRID2023, state output
  emission rates, taken from EPA's published summary tables. The six New
  England states use the New England grid figure instead, because one grid
  serves all of them. A state's figure describes the plants *in* that state,
  so a state that imports much of its power is only roughly described by it.
- *Supplier comparison* exists only for Massachusetts so far. Elsewhere the
  Rates page says it isn't available and still shows your price, its split
  (once you enter your supply price) and its history. Another state is one
  reader and one line in `rates.COVERED`.
- *Bill emails* (forwarded) read Eversource's notices only, and say
  so. Other utilities: upload a file or type bills in.

## More than one home

Most people have one, and for them none of this is visible: no switcher, no new
screen, and their files are where they always were. A second home is something
you add afterwards, from **Add bills → Add another home**.

- **Each home is separate.** Its own bills, usage exports, ZIP code, notes,
  streaks and badges. Nothing is shared, and nothing analysed in one home looks
  at another.
- **Addresses.** The first home is at `/`, `/electric`, and so on, as ever.
  Another is at `/home/<name>/electric`, which can be bookmarked.
- **A switcher** appears in the header at two homes, with **All homes** for the
  combined page. That page adds up spend, usage and carbon; what changed since
  last year stays on each home's own pages, because weather and step changes
  are about one house in one place.
- **Bills that arrive by email** go to the home that
  already holds that utility account. One that matches none - or matches
  several - is held on the Add bills page until you say where it goes. Uploads
  go to the home you are on.
- **Deleting** a home needs its name typed, and only hides it: its bills are
  kept for 30 days and it can be restored, then it is removed for good. The
  first home can be renamed but not deleted.

A home is a folder inside the user's own files (`homes/<id>/`, laid out like
the first), so hosting needs nothing new. Everything is in `store/homes.py`.

## Compared with last year

The top of each fuel page. It needs a usage export (the meter, not the bill)
reaching back more than a year.

| Panel | What it tells you |
|---|---|
| **The box beside the bill** | The latest period against the same weeks a year earlier, weather taken out |
| **Was it you, or was it the weather?** | Every bill's change, split into the two. The bars add up to the change as billed |
| **Where the change in the bill came from** | The same in dollars, over twelve bills, with the rate as a third part. They sum exactly |
| **Something changed** | A stretch where usage stepped up or down, how big, and what it cost. Give it a name and it stops being flagged |
| **Usage follows the cold** (water: **follows the dry**) | Each bill against how cold, or how dry, it was - shown only when the weather actually drives the usage |
| **What specific changes would be worth** | One degree on the thermostat; 100 watts switched off; one watering in four skipped; whatever came on and stayed on |

**How the weather is taken out.** Every home has a temperature below which
the heating comes on, and an amount of fuel it burns for each degree below
that. Both are found from the home's own history rather than assumed. That
gives what the home *should* use in any weather, and the difference between two
periods' expectations is the weather's share of the change between them. The
rest is yours. Where the weather explains little - here it explains 98% of the
gas and 3% of the electricity - the page says so instead of drawing a
relationship that isn't there.

Water follows the dry rather than the cold. Each day a lawn loses water to sun
and air (Open-Meteo's reference evapotranspiration) and gets some back as rain;
what it needed and didn't get is that day's watering need, in inches. A home
that waters uses so many gallons for every inch of it, fitted the same way,
and a dry summer is then the weather's share rather than the household's. A
home that doesn't water shows no slope, and the page says so. The number shown
beside each bill is plain rainfall, which is what people recognise.

**What counts as a change.** A step is looked for in what the weather does
*not* explain, so winter is never "a step up". Three stories are tried - it
stepped and stayed, it stepped and came back, it stepped and settled somewhere
new - each charged for what it adds, and nothing is reported unless the step
explains a good part of the variation and is at least a tenth of normal use.

**Carbon.** Usage times a published factor: EPA eGRID2023 for the New England
grid (0.246 kg CO2e per kWh) and EPA's figure for natural gas (5.3 kg per
therm). The grid figure is an average; a green supply contract would be lower.

### Where the temperatures come from

[Open-Meteo](https://open-meteo.com/)'s historical archive, under CC BY 4.0 -
the page credits it. No key is needed. **Its free service is for
non-commercial use**; charging for the app means their paid plan or another
source.

What leaves the server is a ZIP code, to find the town, and then the town's
position rounded to about a kilometre. Never an address. The ZIP is read off
the usage export, or set by hand where the export carries no address.
Temperatures are fetched once and kept, shared between users in the same town.

`ELEC_WEATHER=off` switches it off. Every comparison still works; it just
can't say how much of a change was the weather.

This is worked out apart from the bills and after them (`/api/insights`), so
a slow or absent weather service never holds up the page.

## What each fuel page shows beneath that

| Panel | What it tells you |
|---|---|
| **The bill** | The current statement and the change vs last month |
| **Streak** | How many months in a row you've gone down - part of the game, lower on the page |
| **Stat tiles** | 12-month total and running average, your lowest statement ever, dollars banked |
| **Level** | A rank driven by cumulative savings against your starting baseline |
| **Bill by statement** | The full trend line with your 12-month average as a reference |
| **Month-over-month** | Green where you beat the prior month, red where you didn't |
| **Money banked** | Running total saved against the baseline |
| **Cost by calendar month** | The seasonal shape you're actually fighting |
| **Same month, last year** | The seasonally fair comparison — fills in past 12 months of history |
| **Achievements** | 18 badges, each with its unlock rule and live progress |

Every chart has a **Table view** toggle with the same numbers, and the app
follows your system light/dark setting with a manual **Theme** override.

## How the numbers are defined

- **A statement** is a `Current Bill` row, dated by the **due date** printed on
  it. A `Payment` row is matched to its statement when the amounts agree and the
  dates are within ~40 days, which is what marks a statement paid.
- **Baseline** is the average of your **first N statements** (default 6, adjustable).
  It's deliberately fixed rather than a trailing average — a trailing baseline
  drifts down as you improve and quietly erases the savings you earned.
- **Banked** is the running sum of `baseline − statement` for every statement
  after the baseline window.
- **Streak** counts consecutive statements, ending at the most recent, that came
  in lower than the one before.
- **Forecast** applies your target percentage cut to each upcoming month's own
  historical average, so it respects the seasonal shape instead of flat-lining
  at the current bill.

### One honest caveat

Electric usage is strongly seasonal. A month-over-month drop from July to
October is partly you and partly the weather. The **same month, last year**
panel is the fair comparison — it needs more than twelve months of history to
populate, so with a single year of data it will show one row and fill in as you
add exports. Treat the streak as a motivator and the year-over-year numbers as
the scoreboard.

## Getting bills in automatically

### What Eversource actually offers

Eversource supports **Green Button**, but the two halves of it are very
different in practice:

| | What you get | Can a script use it? |
|---|---|---|
| **Download My Data** | 13 months of interval (15-min) usage — **kWh**, not just dollars | No. It's behind a portal login, so it's a manual download. |
| **Connect My Data** | The same data over an OAuth API, on a schedule | Only for companies. You must register as a **third party**, start in sandbox, and wait for Eversource to approve you for live access. Eversource MA runs this through UtilityAPI. |

So there's no sanctioned automated feed for an individual pulling their own
data. That rules out the clean API route, and a login bot is still a bad idea
(MFA, CAPTCHA, terms of service, silent breakage).

### The approach that works

1. **Do one Green Button download by hand.** It's worth the two minutes: it
   gets you 13 months of **kWh**, and kWh is the number you actually control.
   Your bill is rate x usage — if Eversource raises rates 8% and you cut usage
   8%, the dashboard shows no progress even though you won.
2. **Automate the monthly dollar figure from your bill-ready email**, which
   needs no portal credentials at all.
3. **Re-download Green Button once a quarter** to refresh usage.

### Usage data (the important part)

Drop any Eversource usage export into `usage/` and the dashboard picks it up —
**the .zip straight from the portal works; no need to unpack it.** Loose `.csv`
files work too, and a zip plus its own extracted CSV won't double-count.
Two layouts are supported, and both are de-duplicated by account and period, so
you can keep adding exports without curating them:

| Export | Gives you | Missing |
|---|---|---|
| **Billing data** (zip download) | start/end dates, kWh, cost | no temperature |
| **Usage history** ("Green Button" CSV) | read date, kWh, cost, **average temperature** | shorter history |

With usage present the dashboard adds:

- **kWh per day** — normalised, so a 34-day period doesn't look like a spike
- **$/kWh** — the half of the bill you don't control
- **The exact split** of every change into a usage part and a rate part
  (`d(cost) = rate_prev x d(kWh) + kWh_now x d(rate)`, which reconciles to the penny)
- A **kWh streak** beside the dollar streak. A rate cut can flatter the dollar
  figure; it cannot move a kWh.

Bills are matched to service periods **by amount**, then sanity-checked on the
lag. Eversource bills about a month in arrears — the statement due 2026-10-16
charges for electricity metered 2026-08-21 to 2026-09-17 — so the month on a
statement is *not* the month you used the power.

Without any usage files the dashboard degrades cleanly to dollars-only.

### Reading a usage export

`intake/greenbutton.py` parses the Eversource MA usage download. That file is **not**
ESPI XML - it's their own CSV with a five-line preamble before the header, and
it carries the columns that matter: `Usage` (kWh), `Number of Days`, `Charge`
and `Average Temperature`.

```bash
python -m metermatters.intake.greenbutton Green_Button.csv
```

It reports kWh per day (fair across billing periods of different lengths), the
implied `$/kWh` rate, and a regression of daily usage on **heating degree
days** from a 65F balance point. The r-squared is the interesting number: high
means the weather is writing your bill, low means the variation is behavioural
- which is the part you can actually change.

> The export is per-account. Check the account number in it matches the one in
> `AccountHistory.csv`, or the two datasets describe different homes.

### Importing from a script

`intake/import_bill.py` is the seam every automated route plugs into. It merges and
de-duplicates, so running it twice is always a no-op.

```bash
python -m metermatters.intake.import_bill --file ~/Downloads/AccountHistory.csv   # a whole export
python -m metermatters.intake.import_bill --amount 248.26 --due 2026-10-16        # one statement
python -m metermatters.intake.import_bill --file export.csv --dry-run             # preview only
cat export.csv | python -m metermatters.intake.import_bill --file -               # stdin
```

Exit codes: `0` imported something, `1` error, `2` nothing new.

### Reading the bill email

> The mailbox polling below (`--imap`) is for a single-user run on your own
> machine. A hosted install never holds a mailbox password: bills arrive by
> forwarding or through Google instead - see
> [Three ways for bills to get in](#three-ways-for-bills-to-get-in).


Eversource's bill-ready email carries the amount, the bill date and the due
date - everything the dashboard needs - and reading your own mailbox needs no
utility credentials. `intake/billmail.py` parses it:

```bash
python -m metermatters.intake.billmail tests/fixtures/bill_ready.eml   # parse one saved .eml
python -m metermatters.intake.billmail --imap                      # poll the last 45 days
python -m metermatters.intake.billmail --imap --all --import       # whole mailbox, and file it
```

`--days` defaults to 45, which is right for the monthly check and wrong for a
backfill - use `--all`, which also switches the folder to `[Gmail]/All Mail`
(a superset of every label, excluding Spam and Trash) and de-duplicates on
Message-ID.

**Electric and gas arrive from the same sender.** `--fuel` defaults to
`electric` and a subject that names neither is skipped, not guessed - filing a
gas bill in the electric history would corrupt every trend. Each bill is filed
under the account its own masked number matches, so a renumbered account keeps
its real identity in the CSV.

The fields live only in the **HTML** part; the plain-text alternative omits the
due date. If either the amount or the due date is missing, it raises rather
than guessing - a wrong due date files the statement in the wrong month and
corrupts every trend silently.

Credentials come from the environment - on a host, from its secret store:

```bash
ELEC_IMAP_USER=you@example.com
ELEC_IMAP_PASS=...        # Gmail needs an app password, not your account password
```

### Two views of the same statements

With more than a couple of years on file the dashboard splits them:

- **`series`** - every statement. The charts, the year-over-year table and the
  seasonal profile draw from this.
- **`game_series`** - the last 24 statements (`game_window_months`). Streaks,
  records, badges and the savings baseline score against *this*.

The split exists because measuring today's bill against 2019 prices makes the
game unwinnable rather than motivating. Rates roughly tripled over that span;
no amount of switching lights off wins that comparison.

**Merged accounts.** Eversource renumbers the account when service is
re-opened, so the same house can appear as two accounts. When accounts never
overlap in time they're treated as one continuous history (`merge_accounts`).
Overlapping accounts are genuinely different meters and are never merged.

### The retention window

The portal serves a **rolling ~36 months** of usage data. Two things get
confused about that, so to be explicit:

**Anything you have already exported is yours permanently.** It sits in
`usage/` on disk. The window moving does not take it back.

**A gap can only open one way:** going so long without exporting that your
*newest saved* period ages out of the window, so a fresh export can no longer
reach back to meet it. That margin is about three years wide, which is why the
check almost never fires.

Separately, statements keep arriving by email every month while kWh and therms
only arrive when you export - so recent months can have a cost and no usage.
That is a freshness matter, not a loss one, and re-exporting fills it in.

```bash
python -m metermatters.intake.greenbutton --staleness      # exit 1 only if a gap is genuinely at risk
```

It runs as part of the monthly check. An account is only reported while it is
still producing statements - the export's own end date looks the same whether
you stopped exporting or the account closed, so the bill history tells them
apart.

### Getting usage in

Only the portal carries kWh and therms, it offers no feed an individual can
use, and signing in takes a person - so usage arrives by upload. Download the
export from the portal and drop the `.zip` on **Add a new export**. Once a
quarter is plenty: the check below tells you if you are ever close to losing
anything.

### Backfilling history

`AccountHistory.csv` only reaches as far back as the portal's payment history.
The usage export carries **cost as well as kWh**, usually much further back:

```bash
python -m metermatters.intake.backfill --dry-run
python -m metermatters.intake.backfill
```

Synthesised rows are marked `BACKFILL` in the Status column, so they're never
confused with statements from the portal. The amounts are Eversource's own;
only the **due date is estimated**, from the median lag between a service
period ending and its statement falling due.

### The recurring check

`tasks/jobs.py` is what keeps everything current. Each run, for every user, it
warns if a usage
export is about to age out of the portal's rolling window, and checks that no
expected bill has gone missing. Forwarded bills need no polling; they arrive on
their own.

It doesn't care what schedules it. Pick whichever your host makes easy:

| How | Set | Suits |
|---|---|---|
| The app runs it itself, daily | `ELEC_SCHEDULER=1` | one always-on instance |
| A scheduler calls `POST /api/tasks/check` | `ELEC_TASK_TOKEN`, sent as `Authorization: Bearer ...` | instances that sleep when idle |
| A scheduled job runs `python -m metermatters.tasks.jobs` | - | platforms with cron jobs sharing the volume |

Run it as often as you like. Imports are idempotent, several workers waking
at once run it once, and the watchdog rations its own notifications. The task
token can start the check and nothing else, and its answer is counts only.

Exit codes: `0` imported something, `1` a step failed, `2` nothing new.

### When a bill doesn't arrive

"Nothing new" means two different things: no bill yet, or a bill we failed to
see because the email template changed or the app password was revoked. From
the inside they look the same, and the dashboard just goes quietly stale.

`tasks/watchdog.py` tells them apart. Each account's next statement is expected one
billing interval after its last - the account's own median gap, not a fixed day
- and about three weeks ahead of its due date, which is when the notice lands.
Five days past that, it's overdue.

```bash
python -m metermatters.tasks.watchdog             # report; exit 1 if something is overdue
python -m metermatters.tasks.watchdog --notify    # ...and notify, at most once a week per missed bill
```

Accounts quiet for over 120 days are closed, not late, and a renumbered
account is never chased once its successor is billing. An overdue bill also
heads the alerts on the overview page.

### Notifications

Users are told about their own bills - a new one, a missing one, a
disconnected mailbox - by email to their account address, and only by email.
Set `ELEC_SMTP_HOST`, `ELEC_SMTP_USER`, `ELEC_SMTP_PASS` and `ELEC_SMTP_FROM`.
Without them nothing is sent and the alerts still show on the overview.

Running it for yourself alone, with no accounts, `tasks/notify.py` also sends to
[ntfy](https://ntfy.sh) (`ELEC_NTFY_URL`) and to the desktop. Those channels
belong to whoever runs the server, so a hosted install never puts a user's
bill on them.

```bash
python -m metermatters.tasks.notify --test        # send a test message and report each channel
```

### Or just do it by hand

Drop any export on **Add a new export** in the dashboard - the statement
history, a usage `.zip` straight from the portal, or a loose usage CSV, several
at once if you like. Each file is sorted by what's inside it: statements are
merged into `AccountHistory.csv`, usage exports are validated and filed in
`usage/`. An export you already have is recognised by content, and a newer one
with the same name is kept beside the old one rather than over it.

### Writes are atomic

The recurring check and the web app both write the history. Every write goes to a
temporary file and is renamed into place, so a crash can't leave a truncated
CSV - which would parse without complaint and simply hold fewer bills - and a
merge holds a lock from read to write, so two at once can't lose a row.

## Layout

The file layout is in the [README](../README.md#where-things-are). A few
things the names there don't say: `analytics/analysis.py` is pure stdlib and
never imports Flask; `intake/ingest.py` is the seam the web upload and the CLI
both go through; `tests/fixtures/` holds your own sample files (never
committed) and `tests/fixtures/synthetic/` the invented ones with the same
shape, which is what a clean checkout tests against.

Run the tests with:

```bash
.venv/bin/python -m unittest discover -s tests -t . -v
```

## What the dashboard shows beyond the bill

**Gas as well as electric.** Eversource emails both from the same address, so
`--fuel` keeps them apart and each is filed under its own account. The
combined view is the honest headline: gas has been 32-61% of the energy bill
depending on the year.

**The always-on floor.** Usage never drops to zero. The 10th percentile of
daily kWh estimates what runs regardless of season - an upper bound, since even
the quietest month contains deliberate use. Only interval data pins it exactly.

**The price half.** Cost per metered unit over time, plus every move above 3%
and what that move alone did to the bill holding usage fixed. Set
`compare_rate` to price the last twelve months against a competitive supplier's
offer.

A bill is a **standing charge plus usage x rate**, so the two are fitted apart
rather than divided. Dividing barely matters for electricity, where usage is
always substantial, and is nonsense for gas: a summer month of 1 therm against
a $10 standing charge reads as "$10 per therm". Months with near-zero usage are
excluded from the quoted range for the same reason.

**Units follow the fuel** - electric exports are metered in kWh, gas in therms,
and the unit is read from the export header rather than assumed.

**Anomalies.** Statements compared against the same calendar month in *nearby*
years - on kWh/day where usage is known, since a price move is not an anomaly
in how you live.

## The raw data view

`/raw` (linked from the dashboard header) is the escape hatch for questions the
charts don't anticipate. Two datasets:

- **Statements & payments** — every row in the CSV, with the derived fields
  attached: metered period, kWh, kWh/day, `$/kWh`, and whether it's verified.
- **Metered usage periods** — every row from the usage exports, including
  average temperature and read type where the export carries them.

Search across all columns, filter by fuel / account / source / date range, sort
by any column, and download exactly what you're looking at as CSV (raw values,
not display formatting, so it drops straight into a spreadsheet).

Every row carries a **Source**: `portal` for the utility's own export, `email`
for a bill notification, `usage-export` for a row synthesised by `intake/backfill.py`.
That column is written into `AccountHistory.csv` itself, so provenance survives
outside the app.

## Two things that quietly corrupt a bill history

Both are detected and surfaced rather than silently charted:

**"Amount due" is not "this month's charge."** The bill emails report the
account balance, which carries anything unpaid. The signature is four or more
consecutive rises then a collapse once a payment lands; summing those months
double-counts the carried part. Detected by that shape, never by size - bills
have tripled since 2018, so a median comparison would just rediscover the trend.

**Not every statement is corroborated.** A statement is marked *verified* when
its amount matched a metered period's own charge exactly. Anything else came
from email alone. The full table shows which is which.

## A note on what's in these files

`AccountHistory.csv` and the exports in `usage/` contain your **name, service
address and account number**. They're fine sitting locally, but add them to
`.gitignore` before pushing this anywhere public.

## Deploying to Cloud Run with Supabase

> Once it is set up, deploying is one command - `./deploy/deploy.sh` - or a
> `git push`. Both run the tests, build the image, and put it on Cloud Run.
> See [deploy/README.md](deploy/README.md) for the pipeline, rolling back and
> reading build logs. What follows here is the first-time setup of the service
> itself.

Cloud Run has no disk and runs several copies of the app at once, so
everything - accounts, statements, uploaded exports, settings - lives in
Postgres. Supabase is used as exactly that: a Postgres database. The app keeps
its own sign-in; Supabase's auth, storage and API are not used.

**1. Create the database.** Make a Supabase project, then copy the
**Transaction pooler** connection string (port 6543) from *Project Settings →
Database*. The app creates its own tables on first start.

**2. Store the secrets, deploy, schedule.**

```bash
export PROJECT=my-gcp-project REGION=us-east1
./deploy/cloudrun.sh secrets     # once: prompts, hidden, straight into Secret Manager
cp deploy/cloudrun.env.example deploy/cloudrun.env
./deploy/cloudrun.sh deploy      # prints the service URL
# put that URL in deploy/cloudrun.env as ELEC_BASE_URL, then:
./deploy/cloudrun.sh deploy
./deploy/cloudrun.sh schedule    # once: Cloud Scheduler runs the daily check
```

**3. Bring your own bills in**, from this machine:

```bash
# sign up in the deployed app first, then
read -s ELEC_DATABASE_URL; export ELEC_DATABASE_URL     # paste the connection string
ELEC_SECRET_KEY=unused-here-but-turns-accounts-on-0123456789 \
  python -m metermatters.store.accounts adopt you@example.com
```

What to know:

- **The tables are in a schema called `elec`, not `public`**, with row-level
  security on. Supabase serves `public` through its API to anyone with the
  project's anon key; this keeps every bill reachable only with the database
  password. Don't add `elec` to the API's exposed schemas.
- **The daily check is a request.** Cloud Run only runs while serving one, so
  `ELEC_SCHEDULER` does nothing useful there; Cloud Scheduler calls
  `/api/tasks/check` instead.
- **It refuses to start on Cloud Run without `ELEC_DATABASE_URL`**, rather
  than keep data on a disk that empties at every restart.
- **Scale.** `--max-instances 3` with a pool of 4 connections each stays far
  inside Supabase's pooler limits. Writes for one user queue behind a
  database lock, so two instances can't overwrite each other.
- **Cost.** Cloud Run scales to zero, and the first request after a quiet
  spell takes a few seconds to wake it. A free Supabase project pauses after
  about a week with no activity - the daily check is activity, so keep the
  schedule on.

To try it against Postgres locally, see the top of `test_postgres.py`.

## Hosting it anywhere else

The app is a container with one volume. Nothing about it is tied to a
particular cloud.

```bash
docker build -t elec-bill .
docker run -p 8080:8080 -v elec-data:/data \
  -e ELEC_SECRET_KEY \
  -e ELEC_BASE_URL=https://bills.example.com \
  -e ELEC_SCHEDULER=1 \
  elec-bill
```

`-e NAME` with no value copies the variable from your shell, so the secret
never appears in the command. Generate one with
`python -c "import secrets; print(secrets.token_urlsafe(48))"`.

That is a complete, working install: people can sign up with an email and
password and upload their bills. Forwarding and Google are each switched on by
adding their settings, below.

**What the host must provide**

- **A persistent volume at `/data`.** Every account, statement and export
  lives there. Without one, a redeploy wipes everything.
- **HTTPS in front.** Sessions and passwords travel with every request.
- **One instance.** The data is files and SQLite under locks, which hold
  across workers on one machine but not across machines.

The image ships with no data in it, and `.dockerignore` is an allow-list so it
can't.

## Accounts

Setting `ELEC_SECRET_KEY` is what turns accounts on; `wsgi.py` refuses to
start without it. Every request then belongs to a signed-in user and reads and
writes that user's folder only - `/data/users/<id>/`.

| | |
|---|---|
| **Signing up** | Email and password, or *Continue with Google* when that is configured |
| **Passwords** | Hashed with argon2id. Never stored, never recoverable |
| **Guessing** | Eight wrong tries in fifteen minutes locks out that address, and that client |
| **Sessions** | A signed cookie: `HttpOnly`, `SameSite=Lax`, `Secure` over https, 30 days |
| **Other sites** | Cross-site writes are refused outright |
| **Leaving** | *Delete this account* removes every bill, export and setting |

Signing in with Google tells the app a name and an email address. It grants
no access to mail and nothing of Google's is stored.

`ELEC_ALLOW_SIGNUP=0` closes sign-ups once your people are in.

**Not built yet:** password reset and confirming that a sign-up address is
real. Both need outgoing email. Until then a forgotten password means deleting
the account's row by hand, and the app defends against someone signing up with
an address that isn't theirs by discarding that password the moment the real
owner signs in through Google.

**Moving a single-user install in.** Sign up in the app, then:

```bash
python -m metermatters.store.accounts adopt you@example.com    # copies; the originals are left alone
python -m metermatters.store.accounts list
```

## Three ways for bills to get in

Every user picks, on the **Add bills** page, and can mix them. A bill that
arrives twice is counted once.

| Way | What the app can see | What it keeps | Effort |
|---|---|---|---|
| **Upload** | The files you give it | Those files | Each bill, by hand |
| **Forwarding** | Only mail your own filter forwards | Nothing but the bills | Set up once |
| **A scan or photo** | That one image, sent to Google's Cloud Vision to read the text | Nothing - the bill you confirm, and not the image | Each bill, with a check |

Upload always exists. The others appear when the operator has configured
them. Usage data - kWh and therms - only ever arrives by upload.

### Forwarding

Each user has a private address, `bills-<token>@<your inbound domain>`. A
filter in their mailbox forwards the utility's notices to it, an inbound email
service turns each message into a request to `POST /api/inbound/email`, and
the app files the bill. It holds no mailbox credential at all.

Set `ELEC_INBOUND_DOMAIN`, then one of:

| Service | Configure it to | Set |
|---|---|---|
| **Mailgun** | a route: *forward* to `https://<app>/api/inbound/email` | `ELEC_MAILGUN_SIGNING_KEY` |
| **Postmark** | webhook `https://any:<secret>@<app>/api/inbound/email`, with *include raw email content* on | `ELEC_INBOUND_SECRET` |
| **Cloudflare Email Routing** (free; what metermatters.app uses) | the worker in `deploy/cloudflare-email-worker.js`, as a catch-all route; its setup steps are at the top of the file | `ELEC_INBOUND_SECRET` |
| Anything else | POST the raw message, `Content-Type: message/rfc822`, `Authorization: Bearer <secret>`, recipient in `X-Envelope-To` | `ELEC_INBOUND_SECRET` |

A message is filed only if the request is authenticated, the address is a
real user's, and it is from the utility's own sender. The utility's DKIM
signature is checked too: `ELEC_INBOUND_DKIM=warn` (the default) files an
unverified bill and says so in the user's log, `require` refuses it, `off`
skips the check. Start on `warn`, look at what real forwarded mail reports,
then move to `require`.

Gmail won't forward to an address until it has been confirmed. The
confirmation message arrives like any other and its link is shown to the user
on the Add bills page - only ever a link to Google's own site.

These adapters are tested against each service's documented format, not
against the live services. Send yourself one bill before relying on it.

### Google

Create an OAuth client in Google Cloud, with the redirect URI
`<ELEC_BASE_URL>/auth/google/callback`, and set `ELEC_GOOGLE_CLIENT_ID` and
`ELEC_GOOGLE_CLIENT_SECRET`. That enables *Continue with Google* and *Import
once*.

*Stay connected* also needs `ELEC_TOKEN_KEY` (`python -m metermatters.store.secrets_box` prints
one), which encrypts the one thing kept: the user's refresh token. The key
lives in your host's secret store, never in the database, so a copy of the
database alone opens nothing. To rotate it, put the new key first and the old
one after a comma.

Reading mail is a *restricted* scope. For your own use an unverified client
works, behind a warning screen. Offering it to the public means Google's
verification and security assessment - check their current policy first.
Sign-in alone needs none of that.

## Config

Everything is an environment variable, and `.env.example` lists them all.
Anything left unset switches its feature off; nothing else breaks.

| Variable | What it does |
|---|---|
| `ELEC_SECRET_KEY` | signs sessions; turns accounts on. Required when hosted |
| `ELEC_BASE_URL` | the address the app is served at |
| `ELEC_DATABASE_URL` | Postgres connection string. Required on Cloud Run |
| `ELEC_DATA_DIR` | where data lives when there is no database (default: the checkout) |
| `ELEC_ALLOW_SIGNUP` | `0` to close sign-ups |
| `ELEC_TRUSTED_PROXIES` | proxies in front of the app (default 1) |
| `ELEC_INBOUND_DOMAIN`, `ELEC_INBOUND_SECRET`, `ELEC_MAILGUN_SIGNING_KEY`, `ELEC_INBOUND_DKIM` | forwarding |
| `ELEC_GOOGLE_CLIENT_ID`, `ELEC_GOOGLE_CLIENT_SECRET` | Google sign-in |
| `ELEC_VISION_KEY` | reading scans and photos of bills (Cloud Vision) |
| `ELEC_SCHEDULER`, `ELEC_TASK_TOKEN` | the recurring check |
| `ELEC_SMTP_*` | email to users |
| `ELEC_WEATHER` | `off` to make no weather lookups (default: on, via Open-Meteo) |
| `PORT`, `WEB_CONCURRENCY` | port and worker count |

`/health` answers whether the app is up and can reach its data. (`/healthz`
is the same check, but Cloud Run answers that path itself and the app never
sees it.)

With no `ELEC_SECRET_KEY`, `./run.sh` is one person on localhost with no
sign-in and the files in the checkout. `DEBUG=1` turns on Flask's reloader.
