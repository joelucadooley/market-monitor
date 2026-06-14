# Market Monitor

A self-updating, terminal-style finance dashboard that runs entirely on GitHub —
free hosting (GitHub Pages) + free data refresh (GitHub Actions). No servers, no
subscriptions. One free API key.

**Three views:**

- **F1 RATES** — global central bank policy rates (Fed, BoE, ECB direct; BoJ, SNB,
  BoC, RBA, Riksbank, Norges, BCB, RBI, BoK, Banxico, RBNZ, SARB via BIS), plus
  SONIA, SOFR, the US Treasury curve and UK 10Y gilt.
- **F2 CREDIT** — ICE BofA option-adjusted spreads (US HY, IG, CCC, Euro HY) with
  trend sparklines and a CCC-vs-HY decompression readout.
- **F3 MBS** — the Big Short view: the full coupon stack inside MBB (iShares MBS
  ETF), aggregated from its daily holdings file, with deep-discount pandemic-era
  pools flagged, plus the 30Y mortgage rate.

Data refreshes twice each weekday via a scheduled workflow that commits fresh
JSON into `data/`. The page is pure static HTML/JS that reads those files.

---

## Setup (one time, ~10 minutes)

### 1. Get a free FRED API key
Register at https://fred.stlouisfed.org/docs/api/api_key.html (instant, free).

### 2. Create the repo and push these files
```bash
# from inside this folder
git init
git add .
git commit -m "initial: market monitor"
# create a PUBLIC repo on github.com named e.g. market-monitor, then:
git remote add origin https://github.com/YOUR_USERNAME/market-monitor.git
git branch -M main
git push -u origin main
```
(Public repo = unlimited free Actions minutes and free Pages. A private repo
also works on the free tier but consumes your monthly Actions quota.)

### 3. Add the FRED key as a secret
Repo → **Settings → Secrets and variables → Actions → New repository secret**
- Name: `FRED_API_KEY`
- Value: your key

### 4. Allow the workflow to push commits
Repo → **Settings → Actions → General → Workflow permissions** →
select **Read and write permissions** → Save.

### 5. Run the workflow once
Repo → **Actions** tab → "Update market data" → **Run workflow**.
Watch the log — each source prints what it fetched. First run takes ~1–2 min.

### 6. Turn on GitHub Pages
Repo → **Settings → Pages** → Source: **Deploy from a branch** →
Branch: `main`, folder `/ (root)` → Save.

A couple of minutes later your dashboard is live at:
`https://YOUR_USERNAME.github.io/market-monitor/`

That's it. It now updates itself on the schedule in
`.github/workflows/update-data.yml` (06:25 and 13:25 UTC, weekdays — edit the
cron lines to taste; note GitHub may delay scheduled runs by a few minutes).

---

## How it works

```
GitHub Actions (cron)
   └── scripts/fetch_data.py
         ├── FRED API ............ Fed/ECB rates, Treasuries, ICE BofA spreads,
         │                         30Y mortgage rate, UK 10Y gilt   [needs key]
         ├── Bank of England IADB . Bank Rate + SONIA               [no key]
         ├── BIS policy-rate API .. 12 more central banks           [no key]
         └── iShares MBB CSV ...... full daily fund holdings        [no key]
               ↓
         writes data/*.json → commits to main
               ↓
GitHub Pages serves index.html, which fetches data/*.json in the browser
```

**Resilience:** every source is wrapped independently. If one is down, the
previous value is kept and shown with a `STALE` badge instead of breaking the
page. The workflow only fails if *every* source fails.

## Troubleshooting

- **Page says "NO DATA YET"** — the workflow hasn't run, or Pages deployed
  before the data commit. Run the workflow, wait for Pages to redeploy (~1 min).
- **STALE badge on one block** — that source failed on the last run. Open the
  Actions log; each fetcher prints its error. BIS occasionally changes its API
  shape — the script tries two endpoint formats; if both 403/404, check
  https://data.bis.org for the current API path and update `fetch_bis()`.
- **MBB stack empty** — iShares sometimes changes CSV column headers. The
  parser matches columns loosely; the Actions log will show what it found.
- **Want different ETFs?** Change `MBB_URL` in `scripts/fetch_data.py`. The URL
  pattern is the fund page + `/1467271812596.ajax?fileType=csv&fileName=TICKER_holdings&dataType=fund`
  — works for HYG (high yield), LQD (IG corporates), etc.
- **Want more/fewer central banks?** Edit `BIS_AREAS` in the script (ISO-2
  country codes).

## Honest limitations

- This is **daily-cadence**, not tick-by-tick. The spread indices and FRED
  series update once a day; iShares holdings once a day; mortgage rate weekly;
  UK 10Y gilt monthly (OECD series).
- Bond-level distressed prints (FINRA TRACE) need a free registered account +
  OAuth and are left as a v2 extension — F2 shows the index-level version.
- Personal/non-commercial use. Each source has its own terms (iShares data in
  particular); don't redistribute the data commercially.
- Nothing here is investment advice.
