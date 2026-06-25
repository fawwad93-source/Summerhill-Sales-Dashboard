"""
daily_inject.py
================
Reads yesterday's sales data from the Summerhill LLC Google Sheet (PPC SHEET tab),
injects new records into Summerhill_Sales_Dashboard.html, and writes the file in-place.

Run by GitHub Actions every day at 4 AM PDT.
Safe guards:
  - Read-only Google Sheets access (Viewer service account)
  - Skips if yesterday already exists in the dashboard
  - Stops parsing at the TARGET section (never reads target data)
  - Validates at least 3 products found before writing
  - Exits cleanly with code 0 if no data yet (sheet not filled)
"""

import json, os, re, sys
from datetime import datetime, timedelta

import gspread
import pytz
from google.oauth2.service_account import Credentials

# ══════════════════════════════════════════════════════════════════════════════
# CONFIG  — edit these if your sheet name or file path changes
# ══════════════════════════════════════════════════════════════════════════════
SPREADSHEET_ID = '1VdmdKDECLFNTonee-AqhysHWQComjVAiXlXb9-7hMsE'
SHEET_TAB      = 'PPC SHEET'
HTML_PATH      = 'index.html'
TIMEZONE       = 'America/Los_Angeles'   # PDT (UTC-7) / PST (UTC-8)

PROD_MAP  = {
    'ZTS':    'ZTS',
    'ZCPM':   'ZCPM',
    'ZCPM 2': 'ZCPM2',
    'ZVHR':   'ZVHR',
    'ZKS':    'ZKS',
}
DAY_NAMES = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']

# Date formats Google Sheets might return when read as a string
DATE_FORMATS = [
    '%d %b %Y',    # "23 Jun 2026"
    '%d/%m/%Y',    # "23/06/2026"
    '%m/%d/%Y',    # "06/23/2026"
    '%Y-%m-%d',    # "2026-06-23"
    '%d-%m-%Y',    # "23-06-2026"
    '%B %d, %Y',   # "June 23, 2026"
    '%d %B %Y',    # "23 June 2026"
    '%d %b %y',    # "23 Jun 26"
]

# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════
def parse_date(s):
    """Try every known date format; return 'YYYY-MM-DD' string or None."""
    s = s.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).strftime('%Y-%m-%d')
        except ValueError:
            pass
    return None


def to_float(s, default=0.0):
    """Convert a Google Sheets cell string to float.
    Handles: $1,234.56  (1,234.56)  -$12.34  #DIV/0!  empty  %
    """
    if not s:
        return default
    s = s.strip()
    if s in ('#DIV/0!', '#REF!', '#VALUE!', '#N/A', '#NAME?', '#NULL!', '#ERROR!'):
        return default
    # Remove currency, commas, spaces, percent
    s = re.sub(r'[$,%\s]', '', s)
    # Parentheses = negative: (5.00) → -5.00
    if s.startswith('(') and s.endswith(')'):
        s = '-' + s[1:-1]
    try:
        return float(s)
    except ValueError:
        return default


def to_int_or_null(s):
    """For stock / rank: return int or None if blank / error."""
    if not s:
        return None
    s = s.strip()
    if s in ('#DIV/0!', '#REF!', '#VALUE!', '#N/A', ''):
        return None
    s = re.sub(r'[,$\s]', '', s)
    try:
        return int(round(float(s)))
    except ValueError:
        return None


def day_name(date_str):
    return DAY_NAMES[datetime.strptime(date_str, '%Y-%m-%d').weekday()]


# ══════════════════════════════════════════════════════════════════════════════
# STEP 1 — Determine target date (yesterday in PDT)
# ══════════════════════════════════════════════════════════════════════════════
tz            = pytz.timezone(TIMEZONE)
now_local     = datetime.now(tz)
yesterday     = (now_local - timedelta(days=1)).strftime('%Y-%m-%d')
print(f'=== Summerhill Dashboard Daily Injector ===')
print(f'Now (PDT):    {now_local.strftime("%Y-%m-%d %H:%M %Z")}')
print(f'Target date:  {yesterday}')

# ══════════════════════════════════════════════════════════════════════════════
# STEP 2 — Connect to Google Sheets (read-only)
# ══════════════════════════════════════════════════════════════════════════════
print('\nConnecting to Google Sheets …')
creds_info = json.loads(os.environ['GOOGLE_CREDENTIALS'])
creds = Credentials.from_service_account_info(
    creds_info,
    scopes=['https://www.googleapis.com/auth/spreadsheets.readonly']
)
gc = gspread.authorize(creds)
ws = gc.open_by_key(SPREADSHEET_ID).worksheet(SHEET_TAB)
all_rows = ws.get_all_values()
print(f'Loaded {len(all_rows)} rows from "{SHEET_TAB}"')

# ══════════════════════════════════════════════════════════════════════════════
# STEP 3 — Parse rows, extract yesterday's records only
# ══════════════════════════════════════════════════════════════════════════════
print(f'\nParsing rows for {yesterday} …')
new_records = []
cur_date    = None

for row_num, row in enumerate(all_rows, start=1):
    # Pad short rows
    while len(row) < 26:
        row.append('')

    col_a = row[0].strip()
    col_b = row[1].strip()

    # ── Stop at the TARGET section ────────────────────────────────────────────
    if col_a.upper() == 'TARGET':
        print(f'  Row {row_num}: Reached TARGET section — stopping parse.')
        break

    # ── Date header row: col_b == 'ITEMS' ────────────────────────────────────
    if col_b == 'ITEMS':
        parsed = parse_date(col_a)
        if parsed:
            cur_date = parsed if parsed == yesterday else None
            status   = 'TARGET DATE ✓' if cur_date else 'skip'
            print(f'  Row {row_num}: Date {parsed} → {status}')
        continue

    # ── Only process rows under the target date ───────────────────────────────
    if cur_date is None:
        continue

    # ── Skip Total / blank rows ───────────────────────────────────────────────
    if col_b in ('Total', 'TOTAL', ''):
        continue

    # ── Product rows ──────────────────────────────────────────────────────────
    if col_b not in PROD_MAP:
        continue

    product   = PROD_MAP[col_b]
    ppc_qty   = int(round(to_float(row[6])))
    total_qty = int(round(to_float(row[8])))
    # Cap ppcQty to totalQty (handles occasional formula errors in sheet)
    ppc_qty   = min(ppc_qty, total_qty)
    org_qty   = max(0, total_qty - ppc_qty)

    rec = {
        'date':          cur_date,
        'day':           day_name(cur_date),
        'product':       product,
        'ppcExpense':    round(to_float(row[2]),  4),
        'clicks':        float(to_float(row[3])),
        'impressions':   float(to_float(row[4])),
        'salePPC':       round(to_float(row[5]),  4),
        'ppcQty':        ppc_qty,
        'orgQty':        org_qty,
        'totalQty':      total_qty,
        'totalSales':    round(to_float(row[9]),  4),
        'sellingPrice':  round(to_float(row[10]), 4),
        'fbaFees':       round(to_float(row[11]), 4),
        'unitCost':      round(to_float(row[12]), 4),
        'profitPerUnit': round(to_float(row[13]), 4),
        'totalProfit':   round(to_float(row[14]), 4),
        'netProfit':     round(to_float(row[15]), 4),
        'stock':         to_int_or_null(row[21]),
        'rank':          to_int_or_null(row[22]),
    }
    new_records.append(rec)
    print(f'  Row {row_num}: {product} → net=${rec["netProfit"]:.2f}, qty={total_qty}')

# ── Nothing found? Sheet not yet filled for this date ────────────────────────
if not new_records:
    print(f'\nNo records found for {yesterday}.')
    print('The sheet may not be filled yet for this date — exiting cleanly.')
    sys.exit(0)

print(f'\nParsed {len(new_records)} product records for {yesterday}')

# ══════════════════════════════════════════════════════════════════════════════
# STEP 4 — Load HTML and check for duplicates
# ══════════════════════════════════════════════════════════════════════════════
print(f'\nLoading {HTML_PATH} …')
with open(HTML_PATH, encoding='utf-8') as f:
    html = f.read()

# Extract BASELINE_DATA array using bracket-depth scan
arr_start = html.index('[', html.find('const BASELINE_DATA = ['))
depth = 0
arr_end = None
for i, ch in enumerate(html[arr_start:], arr_start):
    if ch == '[':
        depth += 1
    elif ch == ']':
        depth -= 1
        if depth == 0:
            arr_end = i + 1
            break

if arr_end is None:
    print('ERROR: Could not find end of BASELINE_DATA array.')
    sys.exit(1)

existing      = json.loads(html[arr_start:arr_end])
existing_dates = {r['date'] for r in existing}
print(f'Existing records: {len(existing)}  ({min(existing_dates)} → {max(existing_dates)})')

# ── Safety check: skip if date already loaded ─────────────────────────────────
if yesterday in existing_dates:
    print(f'\n{yesterday} already exists in the dashboard — nothing to do. Exiting.')
    sys.exit(0)

# ══════════════════════════════════════════════════════════════════════════════
# STEP 5 — Validate parsed data
# ══════════════════════════════════════════════════════════════════════════════
total_net = round(sum(r['netProfit']  for r in new_records), 2)
total_qty = sum(r['totalQty']  for r in new_records)
total_sales = round(sum(r['totalSales'] for r in new_records), 2)

print(f'\nValidation summary for {yesterday}:')
print(f'  Products parsed : {len(new_records)}')
print(f'  Total units     : {total_qty}')
print(f'  Total sales     : ${total_sales:.2f}')
print(f'  Total net profit: ${total_net:.2f}')

if len(new_records) < 3:
    print('\nERROR: Fewer than 3 products parsed — possible column-shift in sheet.')
    print('Aborting to avoid corrupt data.')
    sys.exit(1)

# ══════════════════════════════════════════════════════════════════════════════
# STEP 6 — Merge and write updated HTML
# ══════════════════════════════════════════════════════════════════════════════
em = {(r['date'], r['product']): r for r in existing}
added = replaced = 0
for r in new_records:
    k = (r['date'], r['product'])
    if k in em:
        replaced += 1
    else:
        added += 1
    em[k] = r

merged = sorted(em.values(), key=lambda r: (r['date'], r['product']))
new_html = html[:arr_start] + json.dumps(merged, separators=(',', ':')) + html[arr_end:]

with open(HTML_PATH, 'w', encoding='utf-8') as f:
    f.write(new_html)

print(f'\n✓ Dashboard updated successfully!')
print(f'  Records: {len(existing)} → {len(merged)} (+{added} added, {replaced} replaced)')
print(f'  Date range: {merged[0]["date"]} → {merged[-1]["date"]}')
print(f'  File size: {len(new_html):,} bytes')
