"""
daily_inject.py
================
Reads one day's sales data from the Summerhill LLC Google Sheet (PPC SHEET tab),
injects validated records into index.html, and writes the file in-place.

Run by GitHub Actions every day at 4 AM PDT.

Safeguards:
  - Read-only Google Sheets access (Viewer service account)
  - Parses ONLY the requested date block and stops at that block's Total row
  - Treats any "TARGET..." section as out-of-bounds and never imports it
  - Rejects duplicate product rows inside a date block
  - Cross-checks product totals against the Google Sheet Total row before writing
  - Skips an existing date unless REPLACE_EXISTING=true is explicitly supplied
  - Supports TARGET_DATE=YYYY-MM-DD for one-day backfills/corrections
  - Removes every existing row for a corrected date before inserting the validated replacement block
  - Exits cleanly with code 0 when the target date is not yet present in the sheet
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta

import gspread
import pytz
from google.oauth2.service_account import Credentials

# ══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════════════
SPREADSHEET_ID = '1VdmdKDECLFNTonee-AqhyxHWQComjVAiXlXb9-7hMsE'
SHEET_TAB      = 'PPC SHEET'
HTML_PATH      = 'index.html'
TIMEZONE       = 'America/Los_Angeles'   # PDT (UTC-7) / PST (UTC-8)

PROD_MAP = {
    'ZTS':    'ZTS',
    'ZCPM':   'ZCPM',
    'ZCPM 2': 'ZCPM2',
    'ZVHR':   'ZVHR',
    'ZVHRO':  'ZVHRO',
    'ZKS':    'ZKS',
}

DAY_NAMES = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

DATE_FORMATS = [
    '%d %b %Y',    # 23 Jun 2026
    '%d/%m/%Y',    # 23/06/2026
    '%m/%d/%Y',    # 06/23/2026
    '%Y-%m-%d',    # 2026-06-23
    '%d-%m-%Y',    # 23-06-2026
    '%B %d, %Y',   # June 23, 2026
    '%d %B %Y',    # 23 June 2026
    '%d %b %y',    # 23 Jun 26
]

MIN_PRODUCTS = 3
MAX_ROWS_AFTER_DATE_HEADER = 20
MONEY_TOLERANCE = 0.05
SALES_TOLERANCE_MIN = 0.50
SALES_TOLERANCE_PCT = 0.002   # 0.2%; protects against formatting/rounding only


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════
def parse_date(value):
    """Return YYYY-MM-DD for a recognized displayed Google Sheets date."""
    s = str(value or '').strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).strftime('%Y-%m-%d')
        except ValueError:
            pass
    return None


def to_float(value, default=0.0):
    """Convert a displayed Sheets value to float."""
    if value is None:
        return default
    s = str(value).strip()
    if not s:
        return default
    if s in ('#DIV/0!', '#REF!', '#VALUE!', '#N/A', '#NAME?', '#NULL!', '#ERROR!'):
        return default
    s = re.sub(r'[$,%\s]', '', s)
    if s.startswith('(') and s.endswith(')'):
        s = '-' + s[1:-1]
    try:
        return float(s)
    except ValueError:
        return default


def to_int_or_null(value):
    """For stock / rank: return int, or None when blank/error."""
    if value is None:
        return None
    s = str(value).strip()
    if not s or s in ('#DIV/0!', '#REF!', '#VALUE!', '#N/A', '#NAME?', '#NULL!', '#ERROR!'):
        return None
    s = re.sub(r'[,$\s]', '', s)
    try:
        return int(round(float(s)))
    except ValueError:
        return None


def day_name(date_str):
    return DAY_NAMES[datetime.strptime(date_str, '%Y-%m-%d').weekday()]


def pad_row(row, width=26):
    out = list(row)
    if len(out) < width:
        out.extend([''] * (width - len(out)))
    return out


def is_truthy(value):
    return str(value or '').strip().lower() in ('1', 'true', 'yes', 'y', 'on')


def make_record(row, target_date):
    """Build one dashboard record from a validated product row."""
    row = pad_row(row)

    product = PROD_MAP[row[1].strip()]
    ppc_expense = round(to_float(row[2]), 4)
    clicks = float(to_float(row[3]))
    impressions = float(to_float(row[4]))
    sale_ppc = round(to_float(row[5]), 4)

    ppc_qty = int(round(to_float(row[6])))
    total_qty = int(round(to_float(row[8])))
    if total_qty < 0 or ppc_qty < 0:
        raise ValueError(f'{product}: negative unit quantity is not valid.')
    if ppc_qty > total_qty:
        raise ValueError(f'{product}: PPC quantity ({ppc_qty}) exceeds total quantity ({total_qty}).')
    org_qty = total_qty - ppc_qty

    selling_price = round(to_float(row[10]), 4)
    fba_fees = round(to_float(row[11]), 4)
    unit_cost = round(to_float(row[12]), 4)
    profit_per_unit = round(to_float(row[13]), 4)

    # IMPORTANT:
    # The sheet visually rounds some product-level "Total Sales" cells to whole dollars
    # while the Total row uses exact cents. Derive the exact product total from price × qty
    # when a selling price is available. This keeps dashboard totals aligned with the sheet.
    displayed_total_sales = round(to_float(row[9]), 4)
    if selling_price > 0 or total_qty == 0:
        total_sales = round(selling_price * total_qty, 2)
    else:
        total_sales = displayed_total_sales

    # The sheet's profit formulas are also deterministic from per-unit profit and PPC spend.
    total_profit = round(profit_per_unit * total_qty, 2)
    net_profit = round(total_profit - ppc_expense, 2)

    return {
        'date':          target_date,
        'day':           day_name(target_date),
        'product':       product,
        'ppcExpense':    ppc_expense,
        'clicks':        clicks,
        'impressions':   impressions,
        'salePPC':       sale_ppc,
        'ppcQty':        ppc_qty,
        'orgQty':        org_qty,
        'totalQty':      total_qty,
        'totalSales':    total_sales,
        'sellingPrice':  selling_price,
        'fbaFees':       fba_fees,
        'unitCost':      unit_cost,
        'profitPerUnit': profit_per_unit,
        'totalProfit':   total_profit,
        'netProfit':     net_profit,
        'stock':         to_int_or_null(row[21]),
        'rank':          to_int_or_null(row[22]),
    }


def extract_target_block(all_rows, target_date):
    """
    Find the exact date header and read only that date's product block.

    Parsing ends at the block's Total row. Any TARGET 1/TARGET 2/TARGET 3 area is
    treated as out-of-bounds, so target-planning tables can never be attributed
    to a real date.
    """
    header_index = None

    for idx, original in enumerate(all_rows):
        row = pad_row(original)
        col_a = row[0].strip()
        col_b = row[1].strip()

        # Never search beyond target-planning tables.
        if col_a.upper().startswith('TARGET'):
            break

        if col_b.upper() == 'ITEMS' and parse_date(col_a) == target_date:
            header_index = idx
            break

    if header_index is None:
        return [], None

    print(f'  Row {header_index + 1}: Found exact date header for {target_date}')

    records = []
    seen_products = set()
    total_row = None

    scan_end = min(len(all_rows), header_index + 1 + MAX_ROWS_AFTER_DATE_HEADER)

    for idx in range(header_index + 1, scan_end):
        row = pad_row(all_rows[idx])
        row_num = idx + 1
        col_a = row[0].strip()
        col_b = row[1].strip()
        col_b_upper = col_b.upper()

        # Hard boundary: target-planning area must never be consumed.
        if col_a.upper().startswith('TARGET'):
            raise ValueError(
                f'Row {row_num}: reached "{col_a}" before the daily Total row. '
                'Aborting rather than risk importing target-planning data.'
            )

        # A new date header before Total means the expected block structure changed.
        if col_b_upper == 'ITEMS' and parse_date(col_a):
            raise ValueError(
                f'Row {row_num}: encountered a new date header before the Total row '
                f'for {target_date}. Aborting.'
            )

        if col_b_upper == 'TOTAL':
            total_row = row
            print(f'  Row {row_num}: Total row reached — daily block closed.')
            break

        # Blank spacer rows are allowed inside the short block.
        if not col_b:
            continue

        if col_b not in PROD_MAP:
            # Ignore explanatory/non-product rows, but never silently accept duplicate products.
            continue

        product = PROD_MAP[col_b]
        if product in seen_products:
            raise ValueError(
                f'Row {row_num}: duplicate product {product} found inside {target_date}. '
                'Aborting to prevent a later row from overwriting the real daily row.'
            )

        rec = make_record(row, target_date)
        seen_products.add(product)
        records.append(rec)
        print(
            f'  Row {row_num}: {product} → '
            f'qty={rec["totalQty"]}, sales=${rec["totalSales"]:.2f}, net=${rec["netProfit"]:.2f}'
        )

    if total_row is None:
        raise ValueError(
            f'No Total row found within {MAX_ROWS_AFTER_DATE_HEADER} rows after '
            f'the {target_date} header. Aborting.'
        )

    return records, total_row


def validate_records(records, total_row, target_date):
    """Cross-check parsed product rows against the sheet's daily Total row."""
    if len(records) < MIN_PRODUCTS:
        raise ValueError(
            f'Only {len(records)} product row(s) were parsed for {target_date}; '
            f'at least {MIN_PRODUCTS} are required.'
        )

    products = [r['product'] for r in records]
    if len(products) != len(set(products)):
        raise ValueError('Duplicate products detected after parsing.')

    actual = {
        'ppc': round(sum(r['ppcExpense'] for r in records), 2),
        'qty': sum(r['totalQty'] for r in records),
        'sales': round(sum(r['totalSales'] for r in records), 2),
        'net': round(sum(r['netProfit'] for r in records), 2),
    }
    expected = {
        'ppc': round(to_float(total_row[2]), 2),
        'qty': int(round(to_float(total_row[8]))),
        'sales': round(to_float(total_row[9]), 2),
        'net': round(to_float(total_row[15]), 2),
    }

    print(f'\nValidation summary for {target_date}:')
    print(f'  Products parsed : {len(records)} ({", ".join(products)})')
    print(f'  PPC spend       : ${actual["ppc"]:.2f}  | sheet Total ${expected["ppc"]:.2f}')
    print(f'  Total units     : {actual["qty"]}  | sheet Total {expected["qty"]}')
    print(f'  Total sales     : ${actual["sales"]:.2f}  | sheet Total ${expected["sales"]:.2f}')
    print(f'  Total net profit: ${actual["net"]:.2f}  | sheet Total ${expected["net"]:.2f}')

    problems = []

    if actual['qty'] != expected['qty']:
        problems.append(f'units mismatch ({actual["qty"]} vs {expected["qty"]})')

    if abs(actual['ppc'] - expected['ppc']) > MONEY_TOLERANCE:
        problems.append(f'PPC mismatch (${actual["ppc"]:.2f} vs ${expected["ppc"]:.2f})')

    if abs(actual['net'] - expected['net']) > MONEY_TOLERANCE:
        problems.append(f'net-profit mismatch (${actual["net"]:.2f} vs ${expected["net"]:.2f})')

    sales_tolerance = max(SALES_TOLERANCE_MIN, abs(expected['sales']) * SALES_TOLERANCE_PCT)
    if abs(actual['sales'] - expected['sales']) > sales_tolerance:
        problems.append(
            f'sales mismatch (${actual["sales"]:.2f} vs ${expected["sales"]:.2f}; '
            f'tolerance ${sales_tolerance:.2f})'
        )

    if problems:
        raise ValueError('Validation failed: ' + '; '.join(problems))

    return actual


def extract_baseline_data(html):
    """Return (array_start, array_end, decoded BASELINE_DATA)."""
    marker = 'const BASELINE_DATA = ['
    marker_pos = html.find(marker)
    if marker_pos < 0:
        raise ValueError('Could not find BASELINE_DATA in index.html.')

    arr_start = html.index('[', marker_pos)
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
        raise ValueError('Could not find end of BASELINE_DATA array.')

    return arr_start, arr_end, json.loads(html[arr_start:arr_end])


def main():
    # ── Determine target date ──────────────────────────────────────────────────
    tz = pytz.timezone(TIMEZONE)
    now_local = datetime.now(tz)
    default_date = (now_local - timedelta(days=1)).strftime('%Y-%m-%d')

    explicit_target = os.environ.get('TARGET_DATE', '').strip()
    target_date = explicit_target or default_date
    replace_existing = is_truthy(os.environ.get('REPLACE_EXISTING', ''))

    try:
        datetime.strptime(target_date, '%Y-%m-%d')
    except ValueError:
        print(f'ERROR: TARGET_DATE must use YYYY-MM-DD; received {target_date!r}.')
        sys.exit(1)

    if replace_existing and not explicit_target:
        print('ERROR: REPLACE_EXISTING=true is allowed only with an explicit TARGET_DATE.')
        sys.exit(1)

    print('=== Summerhill Dashboard Daily Injector ===')
    print(f'Now (PDT/PST): {now_local.strftime("%Y-%m-%d %H:%M %Z")}')
    print(f'Target date:   {target_date}' + (' (manual)' if explicit_target else ' (scheduled yesterday)'))
    print(f'Replace mode:  {"ON" if replace_existing else "off"}')

    # ── Connect to Google Sheets (read-only) ───────────────────────────────────
    print('\nConnecting to Google Sheets …')
    try:
        creds_info = json.loads(os.environ['GOOGLE_CREDENTIALS'])
    except KeyError:
        print('ERROR: GOOGLE_CREDENTIALS environment variable is missing.')
        sys.exit(1)
    except json.JSONDecodeError as exc:
        print(f'ERROR: GOOGLE_CREDENTIALS is not valid JSON: {exc}')
        sys.exit(1)

    creds = Credentials.from_service_account_info(
        creds_info,
        scopes=['https://www.googleapis.com/auth/spreadsheets.readonly'],
    )
    gc = gspread.authorize(creds)
    ws = gc.open_by_key(SPREADSHEET_ID).worksheet(SHEET_TAB)
    all_rows = ws.get_all_values()
    print(f'Loaded {len(all_rows)} rows from "{SHEET_TAB}"')

    # ── Parse one exact daily block ────────────────────────────────────────────
    print(f'\nParsing rows for {target_date} …')
    try:
        new_records, total_row = extract_target_block(all_rows, target_date)
        if not new_records:
            print(f'\nNo date block found for {target_date}.')
            print('The sheet may not be filled yet — exiting cleanly with no changes.')
            sys.exit(0)
        validate_records(new_records, total_row, target_date)
    except ValueError as exc:
        print(f'\nERROR: {exc}')
        print('No dashboard file was changed.')
        sys.exit(1)

    # ── Load current HTML ──────────────────────────────────────────────────────
    print(f'\nLoading {HTML_PATH} …')
    with open(HTML_PATH, encoding='utf-8') as f:
        html = f.read()

    try:
        arr_start, arr_end, existing = extract_baseline_data(html)
    except ValueError as exc:
        print(f'ERROR: {exc}')
        sys.exit(1)

    existing_dates = {r['date'] for r in existing}
    print(
        f'Existing records: {len(existing)}  '
        f'({min(existing_dates)} → {max(existing_dates)})'
    )

    # ── Existing-date protection / explicit correction mode ───────────────────
    if target_date in existing_dates and not replace_existing:
        print(
            f'\n{target_date} already exists in the dashboard — nothing to do.\n'
            'To intentionally correct that date, run the workflow manually with '
            'target_date set and replace_existing enabled.'
        )
        sys.exit(0)

    if target_date in existing_dates and replace_existing:
        old_count = sum(1 for r in existing if r['date'] == target_date)
        print(f'\nCorrection mode: removing {old_count} existing record(s) for {target_date}.')
        existing = [r for r in existing if r['date'] != target_date]

    # ── Merge exact validated block ────────────────────────────────────────────
    em = {(r['date'], r['product']): r for r in existing}
    added = replaced = 0

    for r in new_records:
        key = (r['date'], r['product'])
        if key in em:
            replaced += 1
        else:
            added += 1
        em[key] = r

    merged = sorted(em.values(), key=lambda r: (r['date'], r['product']))
    new_html = (
        html[:arr_start]
        + json.dumps(merged, separators=(',', ':'))
        + html[arr_end:]
    )

    with open(HTML_PATH, 'w', encoding='utf-8') as f:
        f.write(new_html)

    print('\n✓ Dashboard updated successfully!')
    print(f'  Records: {len(existing)} → {len(merged)} (+{added} added, {replaced} replaced)')
    print(f'  Date range: {merged[0]["date"]} → {merged[-1]["date"]}')
    print(f'  File size: {len(new_html):,} bytes')


if __name__ == '__main__':
    main()
