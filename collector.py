import json
import re
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone

from nse import NSE

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DATA.mkdir(parents=True, exist_ok=True)
RESULTS_FILE = DATA / "results.json"
CACHE = ROOT / ".nse_cache"
CACHE.mkdir(exist_ok=True)

SLEEP_SECONDS = 0.75
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
        if isinstance(row, dict) and k in row and row[k] not in (None, "", "-"):
            return row[k]
    return None

def bootstrap_companies(nse):
    """
    Do not require data/companies.json to already exist.
    GitHub Actions can create the Nifty 500 master directly from NSE.
    """
    print("Loading current NIFTY 500 constituents from NSE...", flush=True)
    payload = nse.listEquityStocksByIndex(index="NIFTY 500")
    rows = payload.get("data", []) if isinstance(payload, dict) else []

    if not rows:
        raise RuntimeError("NSE returned no NIFTY 500 constituents")

    companies = []
    for i, row in enumerate(rows, 1):
        symbol = pick(row, ["symbol", "Symbol"])
        name = pick(row, ["companyName", "companyname", "Company Name", "meta", "name"]) or symbol
        industry = pick(row, ["industry", "Industry"]) or ""
        isin = pick(row, ["isin", "ISIN", "isinCode"]) or ""
        if symbol:
            companies.append({
                "sl": i,
                "company": str(name).strip(),
                "industry": str(industry).strip(),
                "symbol": str(symbol).strip(),
                "isin": str(isin).strip()
            })

    if len(companies) < 450:
        raise RuntimeError(f"NSE NIFTY 500 response unexpectedly contains only {len(companies)} stocks")

    (DATA / "companies.json").write_text(
        json.dumps(companies, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    print(f"Created data/companies.json with {len(companies)} companies", flush=True)
    return companies

def load_old():
    if not RESULTS_FILE.exists():
        return {}
    try:
        return json.loads(RESULTS_FILE.read_text(encoding="utf-8")).get("results", {})
    except Exception:
        return {}

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
            "tax_lakh": num(pick(row, [
                "re_tax", "re_tax_expense", "tax", "taxExpense"
            ])),
            "other_income_lakh": num(pick(row, [
                "re_other_income", "other_income", "otherIncome"
            ])),
        })
    return out

def latest_xbrl(filings):
    if not isinstance(filings, list):
        return None
    def key(x):
        return str(x.get("toDate") or x.get("periodEnded") or x.get("filingDate") or "")
    for item in sorted(filings, key=key, reverse=True):
        if isinstance(item, dict):
            for k in ("xbrl", "xbrlUrl", "xbrlURL", "xbrl_link"):
                if item.get(k):
                    return item[k]
    return None

def xbrl_enrichment(url, nse):
    if not url:
        return {}
    try:
        path = nse.download_document(url, folder=CACHE)
        raw = Path(path).read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        print(f"    XBRL skipped: {e}", flush=True)
        return {}

    text = re.sub(r"<[^>]+>", " ", raw)
    text = re.sub(r"\s+", " ", text)

    def find_amount(names):
        pattern = r"(?:%s)[^0-9\-\(]{0,180}(\(?-?\d[\d,]*(?:\.\d+)?\)?)" % "|".join(
            re.escape(x) for x in names
        )
        m = re.search(pattern, text, re.I)
        if not m:
            return None
        v = m.group(1).replace(",", "")
        neg = v.startswith("(") and v.endswith(")")
        v = v.strip("()")
        try:
            x = float(v)
            return -x if neg else x
        except Exception:
            return None

    out = {}
    tax = find_amount(["TaxExpense", "IncomeTaxExpense", "CurrentTax", "TaxExpenseCurrent"])
    other = find_amount(["OtherIncome", "OtherIncomeFromOperations", "OtherIncomeExpense"])
    if tax is not None:
        out["tax_lakh"] = tax
    if other is not None:
        out["other_income_lakh"] = other
    return out

old = load_old()
results = {}
ok = 0
failed = 0

print("Starting automatic NSE Nifty 500 update", flush=True)

with NSE(download_folder=CACHE, server=True, timeout=30) as nse:
    # THIS fixes the current GitHub error:
    # data/companies.json is generated automatically if missing.
    companies_file = DATA / "companies.json"
    if companies_file.exists():
        try:
            companies = json.loads(companies_file.read_text(encoding="utf-8"))
            if len(companies) < 450:
                companies = bootstrap_companies(nse)
        except Exception:
            companies = bootstrap_companies(nse)
    else:
        companies = bootstrap_companies(nse)

    total = len(companies)
    print(f"Updating {total} companies", flush=True)

    for i, company in enumerate(companies, 1):
        symbol = company["symbol"]
        previous = old.get(symbol, {})

        rec = {
            "sl": company["sl"],
            "company": company["company"],
            "industry": company.get("industry", ""),
            "symbol": symbol,
            "isin": company.get("isin", ""),
            "status": "error",
            "periods": previous.get("periods", []),
            "source": "NSE India",
            "source_url": f"https://www.nseindia.com/companies-listing/corporate-filings-financial-results?symbol={symbol}&tabIndex=equity",
            "fetched_at": datetime.now(timezone.utc).isoformat()
        }

        success = False

        for attempt in range(RETRIES):
            try:
                cmp = nse.results_comparison(symbol)
                periods = comparison_rows(cmp)

                # Financial Results metadata is supplementary.
                try:
                    filings = nse.financial_results(
                        segment="equities",
                        period="quarterly",
                        symbol=symbol,
                        from_date=datetime.now() - timedelta(days=370),
                        to_date=datetime.now()
                    )
                    xurl = latest_xbrl(filings)
                    if xurl:
                        rec["xbrl_url"] = xurl
                        if periods:
                            enrichment = xbrl_enrichment(xurl, nse)
                            for k, v in enrichment.items():
                                if periods[0].get(k) is None:
                                    periods[0][k] = v
                except Exception as e:
                    rec["filing_error"] = str(e)

                rec["periods"] = periods
                rec["status"] = "ok"
                success = True
                ok += 1
                break

            except Exception as e:
                rec["error"] = str(e)
                if attempt < RETRIES - 1:
                    time.sleep(2 + attempt * 2)

        if not success:
            failed += 1
            if previous:
                rec = previous.copy()
                rec["status"] = "stale"
                rec["fetched_at"] = datetime.now(timezone.utc).isoformat()

        results[symbol] = rec
        print(f"[{i}/{total}] {symbol}: {rec['status']}", flush=True)
        time.sleep(SLEEP_SECONDS)

payload = {
    "updated_at": datetime.now(timezone.utc).isoformat(),
    "source": "NSE India",
    "count": len(results),
    "results": results,
    "stats": {"ok": ok, "failed": failed}
}

tmp = RESULTS_FILE.with_suffix(".tmp")
tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
tmp.replace(RESULTS_FILE)

print(f"FINISHED: {ok} successful, {failed} failed", flush=True)
