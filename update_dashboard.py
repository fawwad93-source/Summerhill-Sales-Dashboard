import urllib.request
import re
import json

# 1. Download the latest CSV data from Google Sheets
SHEET_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQ3Q9IKctA1Jrnbd98AX61H_BNb9O-QaPMxXheL-Sxy6xQS4_-RNZndmSqauPTSoM-rtTbTrMJjTbF3/pub?gid=0&single=true&output=csv" # REPLACE THIS

try:
    response = urllib.request.urlopen(SHEET_URL)
    csv_data = response.read().decode('utf-8').splitlines()
except Exception as e:
    print(f"Failed to download CSV: {e}")
    exit(1)

# 2. Parse the CSV data
new_records = []
headers = []
for i, line in enumerate(csv_data):
    if i == 0:
        headers = [h.strip() for h in line.split(',')]
        continue
    
    parts = [p.strip() for p in line.split(',')]
    if len(parts) < len(headers): continue
    
    # Map your CSV columns to the dashboard's JSON keys here.
    # Adjust the indices (parts[x]) based on your specific Google Sheet structure.
    try:
        record = {
            "date": parts[0],
            "day": parts[1],
            "product": parts[2],
            "ppcExpense": float(parts[3].replace('$', '')) if parts[3] else 0.0,
            "clicks": float(parts[4]) if parts[4] else 0.0,
            "impressions": float(parts[5]) if parts[5] else 0.0,
            "salePPC": float(parts[6].replace('$', '')) if parts[6] else 0.0,
            "ppcQty": float(parts[7]) if parts[7] else 0.0,
            "orgQty": float(parts[8]) if parts[8] else 0.0,
            "totalQty": float(parts[9]) if parts[9] else 0.0,
            "totalSales": float(parts[10].replace('$', '')) if parts[10] else 0.0,
            "sellingPrice": float(parts[11].replace('$', '')) if parts[11] else 0.0,
            "fbaFees": float(parts[12].replace('$', '')) if parts[12] else 0.0,
            "unitCost": float(parts[13].replace('$', '')) if parts[13] else 0.0,
            "profitPerUnit": float(parts[14].replace('$', '')) if parts[14] else 0.0,
            "totalProfit": float(parts[15].replace('$', '')) if parts[15] else 0.0,
            "netProfit": float(parts[16].replace('$', '')) if parts[16] else 0.0,
            "stock": float(parts[17]) if len(parts) > 17 and parts[17] else None,
            "rank": float(parts[18]) if len(parts) > 18 and parts[18] else None
        }
        new_records.append(record)
    except ValueError as e:
        print(f"Skipping row due to parsing error: {line}")
        continue

if not new_records:
    print("No valid records found in the CSV.")
    exit(0)

# 3. Read the existing index.html
with open('index.html', 'r', encoding='utf-8') as f:
    html_content = f.read()

# 4. Extract existing BASELINE_DATA
match = re.search(r'const BASELINE_DATA = (\[.*?\]);', html_content, re.DOTALL)
if not match:
    print("Could not find BASELINE_DATA in index.html")
    exit(1)

existing_data_str = match.group(1)
existing_data = json.loads(existing_data_str)

# 5. Merge datasets (prevent duplicates by using date+product as a unique key)
merged_dict = {}
for r in existing_data:
    merged_dict[r['date'] + '|' + r['product']] = r
for r in new_records:
    merged_dict[r['date'] + '|' + r['product']] = r

final_data = list(merged_dict.values())

# Sort the final data chronologically, then by product
products_order = ['ZTS', 'ZCPM', 'ZCPM2', 'ZVHR', 'ZKS', 'ZLH']
final_data.sort(key=lambda x: (x['date'], products_order.index(x['product']) if x['product'] in products_order else 999))

# 6. Inject the updated data back into HTML
new_baseline_data_str = json.dumps(final_data, separators=(',', ':'))
new_html_content = html_content.replace(match.group(0), f'const BASELINE_DATA = {new_baseline_data_str};')

# Update the "built-in records" date text dynamically based on the newest date
max_date = final_data[-1]['date']
new_html_content = re.sub(r'\(Feb 1–.*?, 2026\)', f'(Feb 1–{max_date})', new_html_content)

# 7. Save the updated HTML
with open('index.html', 'w', encoding='utf-8') as f:
    f.write(new_html_content)

print(f"Successfully updated index.html with data up to {max_date}")
