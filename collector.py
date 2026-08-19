import json
import re
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone

from nse import NSE

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
COMPANIES_FILE = DATA / "companies.json"
RESULTS_FILE = DATA / "results.json"
CACHE = ROOT / ".nse_cache"
CACHE.mkdir(exist_ok=True)

SLEEP_SECONDS = 0.65
RETRIES = 3

def num(v):
    if v is None:
        return None
    s = str(v).strip().replace(",", "").replace("₹", "").replace("%", "")
    if s in ("", "-", "—", "NA", "N/A", "None"):
        return None
    try:
        return float(s)
    except Exception:
        return None

def pick(row, keys):
    for k in keys:
        if k in row and row[k] not in (None, "", "-"):
            return row[k]
    return None

def comparison_rows(payload):
    rows = payload.get("resCmpData") or payload.get("data") or []
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        out.append({
            "period_end": pick(row, ["re_to_dt", "re_to_date", "toDate", "periodEnded", "period_end"]),
            "sales_lakh": num(pick(row, [
                "re_total_inc", "re_total_income", "re_revenue",
                "re_total_revenue", "totalIncome"
            ])),
            "net_profit_lakh": num(pick(row, [
                "re_net_profit", "re_profit_after_tax", "re_pat", "netProfit"
            ])),
            "eps": num(pick(row, [
                "re_eps", "re_diluted_eps", "re_basic_eps", "eps"
            ])),
            # Some NSE response variants expose these directly.
            "tax_lakh": num(pick(row, [
                "re_tax", "re_tax_expense", "tax", "taxExpense"
            ])),
            "other_income_lakh": num(pick(row, [
                "re_other_income", "other_income", "otherIncome"
            ])),
        })
    return out

def xml_enrichment(url, nse):
    """
    Best-effort XBRL enrichment.
    NSE's financial-results metadata can include an XBRL URL.
    Tax/other-income fields are not guaranteed by results_comparison(),
    so they are filled only when an XBRL document exposes a matching fact.
    """
    if not url:
        return {}
    try:
        path = nse.download_document(url, folder=CACHE)
        raw = Path(path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return {}

    # Strip namespaces and inspect XBRL fact tags.
    text = re.sub(r"<[^>]+>", " ", raw)
    text = re.sub(r"\s+", " ", text)

    def find_amount(names):
        pattern = r"(?:%s)[^0-9\-\(]{0,180}(\(?-?\d[\d,]*(?:\.\d+)?\)?)" % "|".join(
            re.escape(x) for x in names
        )
        m = re.search(pattern, text, re.I)
        if not m:
            return None
        v = m.group(1).replace(",", "").strip()
        neg = v.startswith("(") and v.endswith(")")
        v = v.strip("()")
        try:
            x = float(v)
            return -x if neg else x
        except Exception:
            return None

    out = {}
    out["tax_lakh"] = find_amount([
        "TaxExpense", "IncomeTaxExpense", "CurrentTax", "TaxExpenseCurrent"
    ])
    out["other_income_lakh"] = find_amount([
        "OtherIncome", "OtherIncomeFromOperations", "OtherIncomeExpense"
    ])
    return {k:v for k,v in out.items() if v is not None}

def latest_xbrl(filings):
    if not filings:
        return None
    # Prefer filings whose period end is newest.
    def key(x):
        return str(x.get("toDate") or x.get("periodEnded") or x.get("filingDate") or "")
    ordered = sorted(filings, key=key, reverse=True)
    for item in ordered:
        for k in ("xbrl", "xbrlUrl", "xbrlURL", "xbrl_link"):
            if item.get(k):
                return item[k]
    return None

def load_old():
    if not RESULTS_FILE.exists():
        return {}
    try:
        return json.loads(RESULTS_FILE.read_text(encoding="utf-8")).get("results", {})
    except Exception:
        return {}

companies = json.loads(COMPANIES_FILE.read_text(encoding="utf-8"))
old = load_old()
results = {}
ok = 0
failed = 0

print(f"Starting NSE update for {len(companies)} companies", flush=True)

# server=True uses httpx/http2 as recommended for cloud/server environments.
with NSE(download_folder=CACHE, server=True, timeout=30) as nse:
    for i, company in enumerate(companies, 1):
        symbol = company["symbol"]
        previous = old.get(symbol, {})
        rec = {
            "sl": company["sl"],
            "company": company["company"],
            "industry": company["industry"],
            "symbol": symbol,
            "isin": company["isin"],
            "status": "error",
            "periods": previous.get("periods", []),
            "source": "NSE India",
            "source_url": f"https://www.nseindia.com/companies-listing/corporate-filings-financial-results?symbol={symbol}&tabIndex=equity",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

        success = False
        for attempt in range(RETRIES):
            try:
                cmp = nse.results_comparison(symbol)
                periods = comparison_rows(cmp)

                # Filing metadata is used for XBRL/source details.
                xurl = None
                try:
                    filings = nse.financial_results(
                        segment="equities",
                        period="quarterly",
                        symbol=symbol,
                        from_date=datetime.now() - timedelta(days=370),
                        to_date=datetime.now(),
                    )
                    xurl = latest_xbrl(filings)
                except Exception as filing_error:
                    rec["filing_error"] = str(filing_error)

                if xurl:
                    rec["xbrl_url"] = xurl
                    enrichment = xml_enrichment(xurl, nse)
                    if periods:
                        # Only enrich latest row and never overwrite a value
                        # already returned by NSE comparison.
                        for k, v in enrichment.items():
                            if periods[0].get(k) is None:
                                periods[0][k] = v

                rec["periods"] = periods
                rec["status"] = "ok"
                success = True
                ok += 1
                break
            except Exception as e:
                rec["error"] = str(e)
                time.sleep(2 + attempt * 2)

        if not success:
            failed += 1
            # Preserve last known good result rather than deleting it.
            if previous:
                rec = previous.copy()
                rec["status"] = "stale"
                rec["fetched_at"] = datetime.now(timezone.utc).isoformat()

        results[symbol] = rec

        print(
            f"[{i:03d}/{len(companies)}] {symbol:<15} {rec['status']}",
            flush=True
        )

        # Stay below the package's documented NSE request throttling guidance.
        time.sleep(SLEEP_SECONDS)

payload = {
    "updated_at": datetime.now(timezone.utc).isoformat(),
    "source": "NSE India",
    "count": len(results),
    "results": results,
    "stats": {"ok": ok, "failed": failed},
}

tmp = RESULTS_FILE.with_suffix(".tmp")
tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
tmp.replace(RESULTS_FILE)

print(f"Completed: {ok} successful, {failed} failed", flush=True)
