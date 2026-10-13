"""
總經行事曆：爬取美國／台灣官方網站的經濟數據公布時間（不使用任何 API）

來源（皆為公開網頁）：
  美國 BLS     — CPI、PPI、非農就業、JOLTS 等
  美國 BEA     — GDP、個人所得與支出（PCE）
  美國 Census  — 零售銷售、新屋開工、耐久財訂單等
  美國 Fed     — FOMC 利率決策
任何一個來源失敗都只會略過該來源，不會影響每日簡報。
"""

import html as html_lib
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ET = ZoneInfo("America/New_York")
TPE = ZoneInfo("Asia/Taipei")
WINDOW_DAYS = 14

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,zh-TW;q=0.8",
}

MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}
_MONTH_RE = "|".join(MONTHS)
DATE_RE = re.compile(rf"\b({_MONTH_RE})\s+(\d{{1,2}})(?:,\s*(\d{{4}}))?")
TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\s*([AP])\.?M\.?", re.I)

# 只保留市場最關注的指標：(英文關鍵字, 中文名稱)，依序比對、先中先得
US_WATCHLIST = [
    ("Employment Situation",               "非農就業與失業率"),
    ("Consumer Price Index",               "消費者物價指數（CPI）"),
    ("Producer Price Index",               "生產者物價指數（PPI）"),
    ("Job Openings and Labor Turnover",    "職位空缺（JOLTS）"),
    ("Employment Cost Index",              "就業成本指數（ECI）"),
    ("GDP",                                "國內生產毛額（GDP）"),
    ("Personal Income and Outlays",        "個人所得與支出（PCE）"),
    ("International Trade in Goods and Services", "國際貿易收支"),
    ("Advance Monthly Sales for Retail",   "零售銷售"),
    ("New Residential Construction",       "新屋開工與建築許可"),
    ("Advance Report on Durable Goods",    "耐久財訂單"),
    ("New Residential Sales",              "新屋銷售"),
]


def _get(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def _match_watchlist(name):
    for key, zh in US_WATCHLIST:
        if key.lower() in name.lower():
            # 排除 BLS 的子報告（例如 "Consumer Price Index - Average Price Data"）
            if "average price" in name.lower():
                return None
            return zh
    return None


def _infer_year(month, day, today):
    """頁面沒寫年份時，取最接近今天的年份"""
    candidates = []
    for y in (today.year - 1, today.year, today.year + 1):
        try:
            candidates.append(date(y, month, day))
        except ValueError:
            pass
    return min(candidates, key=lambda d: abs((d - today).days)).year


def _to_tpe(d, hh, mm):
    return datetime.combine(d, time(hh, mm), tzinfo=ET).astimezone(TPE)


def _parse_rows(html, source, today, default_time=None):
    """
    通用表格解析：每個 <tr> 裡找「月 日(, 年)」與「hh:mm AM/PM」，
    剩下最長的一格當作發布項目名稱，再用 US_WATCHLIST 過濾。
    """
    soup = BeautifulSoup(html, "html.parser")
    events = []
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        cells = [re.sub(r"\s+", " ", c) for c in cells if c]
        if len(cells) < 2:
            continue
        joined = " | ".join(cells)
        dm = DATE_RE.search(joined)
        if not dm:
            continue
        tm = TIME_RE.search(joined)
        if tm:
            hh = int(tm.group(1)) % 12 + (12 if tm.group(3).upper() == "P" else 0)
            mm = int(tm.group(2))
        elif default_time:
            hh, mm = default_time
        else:
            continue

        name_cells = [
            c for c in cells
            if not DATE_RE.fullmatch(c.strip())
            and not TIME_RE.fullmatch(c.strip())
            and not (DATE_RE.search(c) and TIME_RE.search(c) and len(c) < 30)
        ]
        if not name_cells:
            continue
        name = max(name_cells, key=len)
        zh = _match_watchlist(name)
        if not zh:
            continue

        month, day = MONTHS[dm.group(1)], int(dm.group(2))
        year = int(dm.group(3)) if dm.group(3) else _infer_year(month, day, today)
        try:
            d = date(year, month, day)
        except ValueError:
            continue
        events.append({
            "country": "美國",
            "name": zh,
            "detail": name,
            "dt": _to_tpe(d, hh, mm),
            "source": source,
        })
    return events


def fetch_bls(today):
    events = []
    for y in sorted({today.year, (today + timedelta(days=WINDOW_DAYS)).year}):
        events += _parse_rows(_get(f"https://www.bls.gov/schedule/{y}_sched_list.htm"), "BLS", today)
    return events


def fetch_bea(today):
    return _parse_rows(_get("https://www.bea.gov/news/schedule"), "BEA", today)


def fetch_census(today):
    # 預設頁有時已切到下一年度，所以同時抓當年度頁面
    events, errors = [], []
    for url in ("https://www.census.gov/economic-indicators/calendar-listview.html",
                f"https://www.census.gov/economic-indicators/calendar-listview-{today.year}.html"):
        try:
            events += _parse_rows(_get(url), "Census", today)
        except Exception as e:
            errors.append(e)
    if len(errors) == 2:
        raise errors[0]
    return events


def fetch_fomc(today):
    """FOMC 會議第二天美東 14:00 公布利率決策"""
    soup = BeautifulSoup(_get("https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"),
                         "html.parser")
    events = []
    for panel in soup.find_all(class_="panel"):
        heading = panel.find(class_="panel-heading")
        ym = re.search(r"(\d{4})", heading.get_text()) if heading else None
        if not ym:
            continue
        year = int(ym.group(1))
        for row in panel.find_all(class_="fomc-meeting"):
            m_el = row.find(class_="fomc-meeting__month")
            d_el = row.find(class_="fomc-meeting__date")
            if not m_el or not d_el:
                continue
            # 跨月會議的月份寫成 "Apr/May"，取第二天所在的月份
            month_txt = m_el.get_text(" ", strip=True).split("/")[-1].strip()
            month = next((v for k, v in MONTHS.items() if k.startswith(month_txt[:3])), None)
            days = re.findall(r"\d{1,2}", d_el.get_text())
            if not month or not days or "notation" in d_el.get_text().lower():
                continue
            try:
                d = date(year, month, int(days[-1]))
            except ValueError:
                continue
            sep = "（含經濟預測）" if "*" in d_el.get_text() else ""
            events.append({
                "country": "美國",
                "name": f"FOMC 利率決策{sep}",
                "detail": "FOMC Statement",
                "dt": _to_tpe(d, 14, 0),
                "source": "Fed",
            })
    return events


US_SOURCES = [("BLS", fetch_bls), ("BEA", fetch_bea), ("Census", fetch_census), ("Fed", fetch_fomc)]


def fetch_macro_calendar(today=None):
    """回傳未來兩週（台灣時間）的事件，依時間排序；失敗的來源會被略過"""
    now = datetime.now(TPE)
    today = today or now.date()
    start = datetime.combine(today, time(0, 0), tzinfo=TPE)
    end = start + timedelta(days=WINDOW_DAYS + 1)

    events, failed = [], []
    for name, fn in US_SOURCES:
        try:
            got = fn(today)
            print(f"   [{name}] 解析到 {len(got)} 筆重點指標")
            events += got
        except Exception as e:
            failed.append(name)
            print(f"   ⚠ [{name}] 抓取失敗：{e}")

    seen, result = set(), []
    for e in sorted(events, key=lambda e: e["dt"]):
        key = (e["name"], e["dt"].date())
        if start <= e["dt"] < end and key not in seen:
            seen.add(key)
            result.append(e)
    return {"events": result, "failed": failed, "as_of": now.strftime("%Y-%m-%d %H:%M")}


WEEKDAYS = "一二三四五六日"


def build_calendar_html(cal):
    """首頁最下方的總經行事曆區塊（DYNAMIC:CALENDAR）"""
    rows = ""
    last_day = None
    for e in cal["events"] if cal else []:
        dt = e["dt"]
        day_label = f'{dt.month}/{dt.day}（{WEEKDAYS[dt.weekday()]}）'
        show_day = day_label if day_label != last_day else ""
        last_day = day_label
        tr_class = ' class="cal-newday"' if show_day else ""
        rows += (
            f'          <tr{tr_class}>'
            f'<td class="cal-date">{show_day}</td>'
            f'<td class="cal-time">{dt:%H:%M}</td>'
            f'<td class="cal-country">{e["country"]}</td>'
            f'<td class="cal-name" title="{html_lib.escape(e["detail"])}">{e["name"]}</td>'
            f'<td class="cal-src">{e["source"]}</td></tr>\n'
        )
    if not rows:
        rows = '          <tr><td colspan="5" class="cal-empty">目前沒有抓到未來兩週的公布時間</td></tr>\n'
    as_of = cal["as_of"] if cal else "—"
    return (
        '  <!-- DYNAMIC:CALENDAR:START -->\n'
        '  <section class="macro-calendar" id="macro-calendar">\n'
        '    <div class="cal-header">\n'
        '      <h2 class="cal-title">總經行事曆</h2>\n'
        '      <span class="cal-sub">未來兩週 · 台灣時間</span>\n'
        '    </div>\n'
        '    <div class="cal-table-wrap">\n'
        '      <table class="cal-table">\n'
        '        <thead><tr><th>日期</th><th>時間</th><th>國家</th><th>指標</th><th>來源</th></tr></thead>\n'
        '        <tbody>\n'
        f'{rows}'
        '        </tbody>\n'
        '      </table>\n'
        '    </div>\n'
        f'    <div class="cal-note">資料爬取自各官方網站，更新於 {as_of}。實際公布時間以官方為準。</div>\n'
        '  </section>\n'
        '  <!-- DYNAMIC:CALENDAR:END -->'
    )


if __name__ == "__main__":
    cal = fetch_macro_calendar()
    for e in cal["events"]:
        print(f'{e["dt"]:%m/%d %H:%M}  {e["country"]}  {e["name"]}  ({e["source"]})')
