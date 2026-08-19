NIFTY 500 AUTOMATIC NSE RESULTS — FINAL ARCHITECTURE

What this does:
  1. GitHub Actions runs automatically every 6 hours.
  2. It uses the official NSE corporate-results endpoints through the maintained
     NseIndiaApi Python package.
  3. It fetches results_comparison() for all 500 companies.
  4. It fetches financial-results filing metadata and XBRL links.
  5. It best-effort extracts Tax and Other Income from XBRL when available.
  6. It writes data/results.json and commits it to GitHub.
  7. Your app.mapsx.in dashboard reads that JSON directly from GitHub raw.
  8. Your aaPanel server never connects to NSE.

Files:
  index.html
  config.js
  collector.py
  requirements.txt
  data/companies.json
  data/results.json
  .github/workflows/nse-update.yml

ONE-TIME SETUP:
1. Create a GitHub repository, e.g.:
     nifty500-nse

2. Upload this whole package to the repository.

3. Edit config.js:
     DATA_URL: "https://raw.githubusercontent.com/YOUR_USERNAME/YOUR_REPO/main/data/results.json"

4. Commit.

5. GitHub -> Actions -> "Automatic NSE Nifty 500 Results" -> Run workflow.
   Wait for the first run to finish.

6. Upload only these two files to aaPanel:
     index.html
     config.js

   You do NOT need data/ on aaPanel.

7. Open:
     https://app.mapsx.in/

AUTOMATIC:
The workflow is scheduled every 6 hours at minute 17. It can also be started manually.
GitHub scheduled workflows run from the repository's default branch.

IMPORTANT:
- This uses the unofficial NseIndiaApi wrapper, not an official NSE API SDK.
- NSE remains the source of the underlying financial-result data.
- NSE documents that Results Comparison returns quarterly revenue, net profit and EPS.
- Tax and Other Income are shown only when available from the response/XBRL parser.
- The collector preserves the last successful result if a transient request fails.
- No fake financial values are inserted.

If you prefer the website to remain completely self-contained on aaPanel, use the optional FTP/SFTP deployment workflow instead of raw GitHub.
