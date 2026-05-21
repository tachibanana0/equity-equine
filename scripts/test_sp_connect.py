"""Quick GH Actions SP connectivity test."""
import requests
import time
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ja-JP,ja;q=0.9",
}
SP = "https://db.sp.netkeiba.com"

urls = [
    f"{SP}/race/202605020801/",
    f"{SP}/horse/result/2023106113/",
    f"{SP}/horse/ped/2023106113/",
]

for url in urls:
    t0 = time.time()
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.encoding = r.apparent_encoding or "EUC-JP"
        soup = BeautifulSoup(r.text, "html.parser")
        title = soup.title.string if soup.title else "no title"
        elapsed = time.time() - t0
        print(f"OK {r.status_code} {elapsed:.1f}s [{title}] {url}")
    except Exception as e:
        elapsed = time.time() - t0
        print(f"FAIL {e} {elapsed:.1f}s {url}")

# Test race detail parsing
print("\n--- Race detail test ---")
r = requests.get(f"{SP}/race/202605020801/", headers=HEADERS, timeout=30)
r.encoding = "EUC-JP"
soup = BeautifulSoup(r.text, "html.parser")
table = soup.select_one("table.ResultsByRaceDetail")
if table:
    ths = [th.get_text(strip=True) for th in table.select("thead th")]
    print(f"Columns: {ths}")
    rows = table.select("tbody tr")
    print(f"Horse rows: {len(rows)}")
    if rows:
        tds = rows[0].select("td")
        vals = [td.get_text(strip=True)[:20] for td in tds[:15]]
        print(f"Row 0: {vals}")
else:
    print("Table NOT FOUND")

# Test horse result list
print("\n--- Horse past race IDs test ---")
r = requests.get(f"{SP}/horse/result/2023106113/", headers=HEADERS, timeout=30)
r.encoding = "EUC-JP"
soup = BeautifulSoup(r.text, "html.parser")
for li in soup.select("#ResultsList ul.List_01 li"):
    link = li.select_one("a.LinkBox_Item02")
    if link:
        href = link.get("href", "")
        txt = link.get_text(strip=True)[:80]
        print(f"  {txt}")
        import re
        m = re.search(r"/race/(\d{12})/", href)
        if m:
            print(f"    race_id={m.group(1)}")

# Test pedigree
print("\n--- Pedigree test ---")
r = requests.get(f"{SP}/horse/ped/2023106113/", headers=HEADERS, timeout=30)
r.encoding = "EUC-JP"
soup = BeautifulSoup(r.text, "html.parser")
blood = soup.select_one("section.Blood")
if blood:
    trs = blood.select("table tbody tr")
    male_tds = trs[0].select("td.Male")
    for i, td in enumerate(male_tds):
        link = td.select_one("a[href*='/horse/']")
        name = link.get_text(strip=True) if link else "?"
        classes = td.get("class", [])
        rowspan = td.get("rowspan", "?")
        print(f"  Male[{i}]: {name} (rs={rowspan}, cls={classes})")

    # Damsire
    for i, td in enumerate(trs[0].select("td")):
        rowspan = int(td.get("rowspan", 0))
        if "Female" in td.get("class", []) and rowspan == 8:
            dlink = td.select_one("a[href*='/horse/']")
            dname = dlink.get_text(strip=True) if dlink else "?"
            print(f"  Dam (Female rs=8 td[{i}]): {dname}")
            # Find next Male sibling
            all_tds = trs[0].select("td")
            for j in range(i+1, min(i+6, len(all_tds))):
                if "Male" in all_tds[j].get("class", []):
                    mdlink = all_tds[j].select_one("a[href*='/horse/']")
                    mdname = mdlink.get_text(strip=True) if mdlink else "?"
                    print(f"  Damsire (Male td[{j}] after dam): {mdname}")
                    break
            break
else:
    print("Blood section NOT FOUND")
