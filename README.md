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
Either drag the files into GitHub's web uploader, or use git:
```bash
git init
git add .
git commit -m "initial: market monitor"
git remote add origin https://github.com/YOUR_USERNAME/market-monitor.git
git branch -M main
git push -u origin main
```
Use a PUBLIC repo for unlimited free Actions minutes and free Pages.

### 3. Add the FRED key as a secret
Repo → **Settings → Secrets and variables → Actions → New repository secret**
- Name: `FRED_API_KEY`
- Value: your key

### 4. Allow the workflow to push commits
Repo → **Settings → Actions → General → Workflow permissions** →
select **Read and write permissions** → Save.

### 5. Run the workflow once
Repo → **Actions** tab → "Update market data" → **Run workflow**.
Watch the log — each source prints what it fetched.

### 6. Turn on GitHub Pages
Repo → **Settings → Pages** → Source: **Deploy from a branch** →
Branch: `main`, folder `/ (root)` → Save.

Live at `https://YOUR_USERNAME.github.io/market-monitor/` a minute or two later.

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
page. The workflow only fails if *every* source fails. All values are sanitized
to valid JSON (missing readings become `null`, never `NaN`).

## Troubleshooting

- **Page says "NO DATA YET"** — the workflow hasn't run, or Pages deployed
  before the data commit. Run the workflow, wait for Pages to redeploy.
- **STALE badge on one block** — that source failed on the last run. Open the
  Actions log; each fetcher prints its error.
- **MBB stack empty** — iShares occasionally changes its CSV format. The parser
  extracts the coupon from the security name and guards against HTML error
  pages; the Actions log shows what it found.
- **Different ETFs?** Change `MBB_URL` in `scripts/fetch_data.py`.
- **More/fewer central banks?** Edit `BIS_AREAS` (ISO-2 country codes).

## Honest limitations

- Daily cadence, not tick-by-tick. FRED series and iShares holdings update once
  a day; mortgage rate weekly; UK 10Y gilt monthly.
- Bond-level distressed prints (FINRA TRACE) are a future extension; F2 shows
  the index-level version.
- Personal/non-commercial use. Each source has its own terms.
- Nothing here is investment advice.
