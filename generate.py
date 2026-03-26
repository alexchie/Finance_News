import anthropic
import calendar
import feedparser
import os
import json
import time
from datetime import date

# ── 設定區 ────────────────────────────────────────
# 安全做法：從環境變數讀取，不要直接寫在程式碼裡
# 本機測試時，在終端機執行：export ANTHROPIC_API_KEY="你的key"
API_KEY           = os.environ.get("ANTHROPIC_API_KEY", "")
GA_MEASUREMENT_ID = os.environ.get("GA_MEASUREMENT_ID", "G-0KJ8JBV99C")  # Google Analytics 4 追蹤 ID

TODAY = date.today().strftime("%Y-%m-%d")
TODAY_DISPLAY = date.today().strftime("%Y 年 %#m 月 %#d 日")
OUTPUT_PATH = f"briefings/{TODAY}.html"

TODAY_CUTOFF = time.time() - 86400  # 24小時前的 POSIX timestamp
SCAN_PER_FEED = 8                    # 每個 feed 最多掃描的條數（過濾後數量會減少）

# ── RSS 來源 ───────────────────────────────────────
FEEDS = {
    "金融市場（總經）": [
        "https://feeds.reuters.com/reuters/businessNews",        # Reuters 商業
        "https://www.theguardian.com/business/economics/rss",   # Guardian 經濟
        "https://feeds.bbci.co.uk/news/business/rss.xml",       # BBC 商業
        "https://www.cnbc.com/id/10000664/device/rss/rss.html", # CNBC Economy
        "https://feeds.marketwatch.com/marketwatch/realtimeheadlines/", # MarketWatch 即時
        "https://feeds.apnews.com/rss/business",                # AP Business
        "https://www.ft.com/?format=rss",                       # Financial Times
        "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",       # WSJ Markets
    ],
    "國際大公司重大新聞": [
        "https://feeds.reuters.com/reuters/companyNews",         # Reuters 公司
        "https://www.theguardian.com/business/rss",             # Guardian 商業
        "https://feeds.bbci.co.uk/news/business/rss.xml",       # BBC 商業
        "https://www.cnbc.com/id/10001147/device/rss/rss.html", # CNBC Earnings
        "https://feeds.marketwatch.com/marketwatch/topstories/",# MarketWatch Top
        "https://techcrunch.com/feed/",                         # TechCrunch
        "https://www.cnbc.com/id/15839135/device/rss/rss.html", # CNBC Tech
    ],
    "台灣財經": [
        "https://money.udn.com/rssfeed/news/1001/5588/rss.xml", # 經濟日報 財經
    ],
    # 深度分析：不套用24小時過濾（週刊 + 長文深度分析）
    "深度分析": [
        "https://www.economist.com/leaders/rss.xml",             # Economist Leaders
        "https://www.economist.com/briefing/rss.xml",            # Economist Briefing
        "https://www.project-syndicate.org/rss",                # Project Syndicate
    ],
}

ARTICLES_PER_TOPIC = {
    "金融市場（總經）":   5,
    "國際大公司重大新聞": 3,
    "台灣財經":          2,
}
# ──────────────────────────────────────────────────


def _ga_script():
    """回傳 Google Analytics GA4 script tag；若未設定 GA_MEASUREMENT_ID 則回傳空字串"""
    if not GA_MEASUREMENT_ID:
        return ""
    return f"""  <!-- Google Analytics GA4 -->
  <script async src="https://www.googletagmanager.com/gtag/js?id={GA_MEASUREMENT_ID}"></script>
  <script>
    window.dataLayer = window.dataLayer || [];
    function gtag(){{dataLayer.push(arguments);}}
    gtag('js', new Date());
    gtag('config', '{GA_MEASUREMENT_ID}');
  </script>"""


def is_within_24h(entry) -> bool:
    """判斷 RSS entry 是否為過去24小時內發布。無日期資訊時回傳 True（fail-open）。"""
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if t is None:
        return True
    try:
        # calendar.timegm 正確將 UTC struct_time 轉為 POSIX timestamp
        # （不同於 time.mktime，後者會誤當本地時間導致時區偏差）
        return calendar.timegm(t) >= TODAY_CUTOFF
    except Exception:
        return True


def fetch_articles_by_topic():
    """依主題抓 RSS，只保留過去24小時內的文章，回傳 dict: {主題: [文章列表]}"""
    result = {}
    for topic, urls in FEEDS.items():
        if topic == "深度分析":
            continue  # 深度分析由 fetch_deep_analysis_articles() 另行處理
        articles = []
        for url in urls:
            try:
                feed = feedparser.parse(url)
                for entry in feed.entries[:SCAN_PER_FEED]:
                    if not is_within_24h(entry):
                        continue
                    articles.append({
                        "title": entry.get("title", ""),
                        "summary": entry.get("summary", "")[:250],
                        "link": entry.get("link", ""),
                        "source": feed.feed.get("title", "Unknown"),
                    })
            except Exception as e:
                print(f"   ⚠ 抓取失敗：{url} ({e})")
        result[topic] = articles
        print(f"   [{topic}] 抓到 {len(articles)} 則（24小時內）")
    return result


def fetch_deep_analysis_articles():
    """抓 The Economist Leaders/Briefings，不過濾日期，讓 Claude 選最具分析深度的一篇"""
    articles = []
    for url in FEEDS["深度分析"]:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:10]:
                articles.append({
                    "title": entry.get("title", ""),
                    "summary": entry.get("summary", "")[:350],
                    "link": entry.get("link", ""),
                    "source": feed.feed.get("title", "The Economist"),
                })
        except Exception as e:
            print(f"   ⚠ 深度分析抓取失敗：{url} ({e})")
    print(f"   [深度分析] 抓到 {len(articles)} 則候選")
    return articles


def fetch_market_data():
    """
    抓取美股三大指數 + 台灣加權指數的前一交易日收盤資料。
    執行於 UTC 0:30（台灣 8:30），美股已收盤，台股未開盤。
    失敗時靜默回傳 None，不中斷主流程。
    """
    try:
        import yfinance as yf
    except ImportError:
        print("   ⚠ yfinance 未安裝，跳過市場數據")
        return None

    INDICES = [
        ("^GSPC", "S&P 500"),
        ("^DJI",  "道瓊工業"),
        ("^IXIC", "那斯達克"),
        ("^TWII", "台灣加權"),
    ]
    results = []
    for symbol, name in INDICES:
        try:
            hist = yf.Ticker(symbol).history(period="5d")
            if len(hist) < 2:
                print(f"   ⚠ {name} 歷史資料不足，跳過")
                continue
            close = round(float(hist.iloc[-1]["Close"]), 2)
            prev  = round(float(hist.iloc[-2]["Close"]), 2)
            chg   = round(close - prev, 2)
            pct   = round(chg / prev * 100, 2) if prev else 0.0
            date_str = hist.index[-1].strftime("%Y-%m-%d")
            results.append({
                "name": name, "close": close,
                "change": chg, "change_pct": pct,
                "date": date_str,
            })
        except Exception as e:
            print(f"   ⚠ {name}（{symbol}）數據失敗：{e}")

    if not results:
        return None
    return {"indices": results, "as_of": results[0]["date"]}


def build_prompt(topic_articles, deep_articles):
    """把三個主題的文章與深度分析候選整理成給 Claude 的 prompt"""

    articles_block = ""
    for topic, articles in topic_articles.items():
        articles_block += f"\n## 主題：{topic}\n"
        for i, a in enumerate(articles, 1):
            articles_block += f"{i}. [{a['source']}] {a['title']}\n{a['summary']}\n連結：{a['link']}\n\n"

    deep_block = "\n## 深度分析候選文章（The Economist，請選一篇最具分析深度的）\n"
    for i, a in enumerate(deep_articles, 1):
        deep_block += f"{i}. [{a['source']}] {a['title']}\n{a['summary']}\n連結：{a['link']}\n\n"

    macro_count    = ARTICLES_PER_TOPIC["金融市場（總經）"]
    corp_count     = ARTICLES_PER_TOPIC["國際大公司重大新聞"]
    taiwan_count   = ARTICLES_PER_TOPIC["台灣財經"]

    prompt = f"""你是一位專業的國際金融市場分析師，負責每日撰寫一份給機構投資人與財經從業人員閱讀的情報簡報。

今天是 {TODAY}，以下是從各大財經媒體抓到的新聞（各主題僅含過去24小時內發布的報導）：

{articles_block}
{deep_block}
---

請完成以下工作，全部用**繁體中文**撰寫：

**一、主題新聞分析**
- 金融市場（總經）：選出最重要的 {macro_count} 則（不夠則選有的）
- 國際大公司重大新聞：選出最重要的 {corp_count} 則（不夠則選有的）
- 台灣財經：選出最重要的 {taiwan_count} 則（不夠則選有的）

每則新聞包含：
1. **事件背景介紹**：這件事發生的來龍去脈、相關歷史背景（3-4句）
2. **事件內容及意義**：具體發生了什麼，為什麼重要，對市場的直接影響（3-4句）
3. **詳細分析及研究**：結合歷史數據、市場先例、總體經濟邏輯，深入分析中長期含義（4-6句，需有數據或歷史案例支撐）
4. **country**：這篇新聞主要涉及的國家或地區（1-2個字，例如：美國、歐盟、中國、台灣、日本）

**二、今日概覽**
- overview.topics_covered：三個主題（金融市場、大公司、台灣）各用5-10字的短語概括今日核心動向

**三、深度分析**
從 The Economist / Project Syndicate 候選文章中選出最具分析深度的一篇（不限24小時），完成：
1. **事件背景介紹**（3-4句）
2. **事件內容及意義**（3-4句）
3. **詳細分析及研究**（4-6句）
4. **key_takeaway**：針對此議題的深度洞見，結合歷史視野、制度邏輯、中長期結構性含義（**6-8句**，這是本篇的核心價值，請務必深入）

請用以下 JSON 格式回覆，不要有任何說明文字或 markdown 代碼框：

{{
  "issue_title": "今日整體金融市場的核心主題（一句話）",
  "issue_summary": "今日市場整體局勢的總結（3句話，涵蓋各主題的連動關係）",
  "overview": {{
    "topics_covered": ["金融市場今日核心短語", "大公司今日核心短語", "台灣財經今日核心短語"]
  }},
  "topics": [
    {{
      "topic_name": "金融市場（總經）",
      "articles": [
        {{
          "title": "新聞繁體中文標題",
          "country": "美國",
          "background": "事件背景介紹內容",
          "content": "事件內容及意義內容",
          "analysis": "詳細分析及研究內容",
          "source_name": "來源媒體名稱",
          "source_url": "原始連結"
        }}
      ]
    }},
    {{
      "topic_name": "國際大公司重大新聞",
      "articles": [
        {{
          "title": "新聞繁體中文標題",
          "country": "美國",
          "background": "事件背景介紹內容",
          "content": "事件內容及意義內容",
          "analysis": "詳細分析及研究內容",
          "source_name": "來源媒體名稱",
          "source_url": "原始連結"
        }}
      ]
    }},
    {{
      "topic_name": "台灣財經",
      "articles": [
        {{
          "title": "新聞繁體中文標題",
          "country": "台灣",
          "background": "事件背景介紹內容",
          "content": "事件內容及意義內容",
          "analysis": "詳細分析及研究內容",
          "source_name": "來源媒體名稱",
          "source_url": "原始連結"
        }}
      ]
    }},
    {{
      "topic_name": "深度分析",
      "articles": [
        {{
          "title": "文章繁體中文標題",
          "country": "美國",
          "background": "事件背景介紹",
          "content": "事件內容及意義",
          "analysis": "詳細分析及研究",
          "key_takeaway": "深度洞見（6-8句）",
          "source_name": "The Economist",
          "source_url": "原始連結"
        }}
      ]
    }}
  ]
}}"""

    return prompt


def analyze_with_claude(topic_articles, deep_articles):
    """呼叫 Claude API 進行分析"""
    client = anthropic.Anthropic(api_key=API_KEY)
    prompt = build_prompt(topic_articles, deep_articles)

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=16000,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = message.content[0].text

    # 清除可能的 markdown 代碼框
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1]
        raw = raw.rsplit("```", 1)[0]

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"\n❌ JSON 解析失敗：{e}")
        print(f"Claude 回傳的原始內容（最後200字）：\n{raw[-200:]}")
        raise


def generate_article_html(article, article_id=None):
    """把單篇文章轉成 HTML（標題為可點擊連結）"""
    id_attr = f' id="{article_id}"' if article_id else ""
    return f"""
    <div class="news-item"{id_attr}>
      <h3><a href="{article['source_url']}" target="_blank" rel="noopener">{article['title']}</a></h3>

      <div class="section">
        <div class="section-label">事件背景</div>
        <p>{article['background']}</p>
      </div>

      <div class="section">
        <div class="section-label">事件內容及意義</div>
        <p>{article['content']}</p>
      </div>

      <div class="section analysis">
        <div class="section-label">詳細分析及研究</div>
        <p>{article['analysis']}</p>
      </div>

      <div class="source">來源：<a href="{article['source_url']}" target="_blank">{article['source_name']}</a></div>
    </div>
"""


def generate_deep_article_html(article, article_id=None):
    """把深度分析文章轉成 HTML（多一個 key_takeaway 欄位）"""
    id_attr = f' id="{article_id}"' if article_id else ""
    return f"""
    <div class="news-item deep-item"{id_attr}>
      <h3><a href="{article['source_url']}" target="_blank" rel="noopener">{article['title']}</a></h3>

      <div class="section">
        <div class="section-label">事件背景</div>
        <p>{article['background']}</p>
      </div>

      <div class="section">
        <div class="section-label">事件內容及意義</div>
        <p>{article['content']}</p>
      </div>

      <div class="section analysis">
        <div class="section-label">詳細分析及研究</div>
        <p>{article['analysis']}</p>
      </div>

      <div class="section key-takeaway">
        <div class="section-label">深度洞察</div>
        <p>{article['key_takeaway']}</p>
      </div>

      <div class="source">來源：<a href="{article['source_url']}" target="_blank">{article['source_name']}</a></div>
    </div>
"""


def generate_html(data, market_data=None):
    """把完整 JSON 資料轉成 HTML 頁面"""

    # ── 各 topic 對應的 anchor ID 與文章 ID 前綴 ────────
    SECTION_IDS = {
        "金融市場（總經）":   "section-macro",
        "國際大公司重大新聞": "section-corporate",
        "台灣財經":          "section-taiwan",
        "深度分析":          "section-deep",
    }
    ARTICLE_PREFIXES = {
        "金融市場（總經）":   "macro",
        "國際大公司重大新聞": "corporate",
        "台灣財經":          "taiwan",
        "深度分析":          "deep",
    }

    # ── 生成各 topic 的 HTML ───────────────────────────
    topics_html = ""
    total_count = 0

    for topic in data["topics"]:
        topic_name = topic["topic_name"]
        articles = topic.get("articles", [])
        is_deep = (topic_name == "深度分析")
        prefix = ARTICLE_PREFIXES.get(topic_name, "other")

        if not is_deep:
            total_count += len(articles)

        articles_html = ""
        for i, a in enumerate(articles):
            art_id = f"article-{prefix}-{i}"
            if is_deep:
                articles_html += generate_deep_article_html(a, article_id=art_id)
            else:
                articles_html += generate_article_html(a, article_id=art_id)

        section_id = SECTION_IDS.get(topic_name, "")
        id_attr = f' id="{section_id}"' if section_id else ""
        extra_class = " deep-analysis-section" if is_deep else ""
        deep_badge = ' <span class="deep-badge">IN-DEPTH</span>' if is_deep else ""

        topics_html += f"""
    <section class="topic-section{extra_class}"{id_attr}>
      <div class="topic-header">
        <h2 class="topic-title">{topic_name}{deep_badge}</h2>
      </div>
      {articles_html}
    </section>
"""

    # ── 生成今日概覽區塊 ──────────────────────────────
    overview = data.get("overview", {})
    topics_covered = overview.get("topics_covered", [])

    # Layer 1：市場指數（有數據才顯示）
    if market_data and market_data.get("indices"):
        index_cards_html = ""
        for idx in market_data["indices"]:
            p = idx["change_pct"]
            arrow = "▲" if p > 0 else ("▼" if p < 0 else "—")
            change_cls = "positive" if p > 0 else ("negative" if p < 0 else "neutral")
            # 格式化收盤價（千分位）
            close_fmt = f"{idx['close']:,.2f}"
            change_fmt = f"{arrow}{abs(p):.2f}%"
            index_cards_html += f"""
          <div class="index-card">
            <div class="index-name">{idx['name']}</div>
            <div class="index-close">{close_fmt}</div>
            <div class="index-change {change_cls}">{change_fmt}</div>
          </div>"""
        market_block = f"""
      <div class="market-indices">
        <div class="market-indices-label">市場行情 · {market_data['as_of']} 收盤</div>
        <div class="market-indices-grid">{index_cards_html}
        </div>
      </div>"""
    else:
        market_block = ""

    # Layer 3：主題 TOC + 各主題的文章標題列表（含國家標籤）
    toc_topic_names = ["金融市場（總經）", "國際大公司重大新聞", "台灣財經", "深度分析"]

    # 建立 {topic_name: [articles]} 的快速查找
    topic_articles_map = {t["topic_name"]: t.get("articles", []) for t in data["topics"]}

    toc_rows_html = ""
    for i, tname in enumerate(toc_topic_names):
        anchor = SECTION_IDS.get(tname, "#")
        prefix = ARTICLE_PREFIXES.get(tname, "other")
        is_deep_row = (tname == "深度分析")
        row_class = " deep" if is_deep_row else ""
        theme = topics_covered[i] if i < len(topics_covered) else ""

        # 文章標題列表（含國家標籤）
        articles_in_topic = topic_articles_map.get(tname, [])
        title_links_html = ""
        for j, a in enumerate(articles_in_topic):
            art_id = f"article-{prefix}-{j}"
            country = a.get("country", "")
            country_tag = f'<span class="country-tag">{country}</span>' if country else ""
            title_links_html += f'{country_tag}<a href="#{art_id}" class="toc-article-link">{a["title"]}</a>\n          '

        articles_block = f"""
        <div class="toc-articles">{title_links_html}
        </div>""" if articles_in_topic else ""

        toc_rows_html += f"""
        <div class="toc-row{row_class}">
          <a href="#{anchor}" class="toc-label">{tname}</a>
          <span class="toc-theme">{theme}</span>
        </div>{articles_block}"""

    overview_html = f"""
    <div class="overview-section">
      <div class="overview-label">今日概覽</div>
      {market_block}
      <div class="overview-toc">{toc_rows_html}
      </div>
    </div>
"""

    html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{TODAY} · Daily Briefing</title>
{_ga_script()}
  <style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}

    body {{
      font-family: 'Georgia', serif;
      background: #ffffff;
      color: #111111;
      line-height: 1.9;
    }}

    header {{
      border-bottom: 2px solid #111;
      padding: 2rem 2rem 1.5rem;
      display: flex;
      justify-content: space-between;
      align-items: baseline;
    }}
    header h1 {{ font-size: 1.2rem; letter-spacing: 0.1em; }}
    header nav a {{
      text-decoration: none; color: #111;
      margin-left: 2rem; font-size: 0.9rem;
    }}
    header nav a:hover {{ text-decoration: underline; }}

    .article {{
      max-width: 740px;
      margin: 0 auto;
      padding: 3rem 2rem 5rem;
    }}

    .breadcrumb {{
      font-size: 0.8rem; color: #aaa; margin-bottom: 2rem;
    }}
    .breadcrumb a {{ color: #aaa; text-decoration: none; }}
    .breadcrumb a:hover {{ text-decoration: underline; }}

    /* 期數標題 */
    .issue-meta {{
      border-bottom: 2px solid #111;
      padding-bottom: 1.5rem;
      margin-bottom: 2rem;
    }}
    .issue-meta .date {{
      font-size: 0.8rem; color: #aaa;
      letter-spacing: 0.1em; margin-bottom: 0.5rem;
    }}
    .issue-meta h2 {{
      font-size: 1.9rem; line-height: 1.3; margin-bottom: 0.8rem;
    }}
    .issue-meta .summary {{
      font-size: 0.95rem; color: #444;
      border-left: 3px solid #111;
      padding-left: 1rem;
    }}

    /* 今日概覽 */
    .overview-section {{
      background: #f8f8f8;
      border: 1px solid #e0e0e0;
      padding: 1.5rem 1.8rem;
      margin-bottom: 3rem;
    }}
    .overview-label {{
      font-size: 0.7rem;
      letter-spacing: 0.2em;
      text-transform: uppercase;
      color: #aaa;
      margin-bottom: 1rem;
    }}

    /* 市場指數 */
    .market-indices {{
      margin-bottom: 1.4rem;
      padding-bottom: 1.2rem;
      border-bottom: 1px solid #e8e8e8;
    }}
    .market-indices-label {{
      font-size: 0.68rem;
      letter-spacing: 0.18em;
      text-transform: uppercase;
      color: #aaa;
      margin-bottom: 0.7rem;
    }}
    .market-indices-grid {{
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 0.7rem;
    }}
    @media (max-width: 600px) {{
      .market-indices-grid {{ grid-template-columns: repeat(2, 1fr); }}
    }}
    .index-card {{
      background: #fff;
      border: 1px solid #e0e0e0;
      padding: 0.65rem 0.85rem;
    }}
    .index-name {{
      font-size: 0.68rem;
      color: #999;
      letter-spacing: 0.04em;
      margin-bottom: 0.2rem;
    }}
    .index-close {{
      font-size: 1rem;
      font-weight: bold;
      font-variant-numeric: tabular-nums;
      letter-spacing: 0.01em;
    }}
    .index-change {{
      font-size: 0.7rem;
      font-variant-numeric: tabular-nums;
      margin-top: 0.12rem;
    }}
    .index-change.positive {{ color: #2a7a2a; }}
    .index-change.negative {{ color: #c0392b; }}
    .index-change.neutral  {{ color: #999; }}

    .overview-regions {{
      display: flex; flex-wrap: wrap; gap: 0.5rem;
      margin-bottom: 1.2rem;
    }}
    .country-tag {{
      display: inline-block;
      background: #111; color: #fff;
      font-size: 0.65rem; padding: 0.1rem 0.45rem;
      margin-right: 0.4rem;
      letter-spacing: 0.04em;
      vertical-align: middle;
    }}
    .region-tag {{
      background: #111; color: #fff;
      font-size: 0.72rem; padding: 0.2rem 0.6rem;
      letter-spacing: 0.05em;
    }}
    .overview-toc {{ display: flex; flex-direction: column; gap: 0.3rem; }}
    .toc-row {{ display: flex; align-items: baseline; gap: 0.8rem; margin-top: 0.4rem; }}
    .toc-label {{
      font-size: 0.75rem; font-weight: bold;
      color: #111; text-decoration: none;
      min-width: 165px; flex-shrink: 0;
    }}
    .toc-label:hover {{ text-decoration: underline; }}
    .toc-theme {{ font-size: 0.88rem; color: #555; }}
    .toc-row.deep .toc-label {{ color: #8B4513; }}

    /* 文章標題列表（概覽內） */
    .toc-articles {{
      margin: 0.2rem 0 0.5rem 165px;
      display: flex;
      flex-direction: column;
      gap: 0.2rem;
    }}
    @media (max-width: 600px) {{
      .toc-articles {{ margin-left: 0; }}
    }}
    .toc-article-link {{
      font-size: 0.81rem;
      color: #666;
      text-decoration: none;
      line-height: 1.5;
    }}
    .toc-article-link::before {{ content: "· "; color: #bbb; }}
    .toc-article-link:hover {{ color: #111; text-decoration: underline; }}

    /* 主題區塊 */
    .topic-section {{
      margin-bottom: 4rem;
    }}
    .topic-header {{
      border-bottom: 1px solid #111;
      padding-bottom: 0.5rem;
      margin-bottom: 1.5rem;
      display: flex;
      align-items: center;
    }}
    .topic-title {{
      font-size: 0.8rem;
      letter-spacing: 0.2em;
      text-transform: uppercase;
      font-weight: normal;
      color: #555;
    }}

    /* 單篇新聞 */
    .news-item {{
      padding: 2rem 0;
      border-bottom: 1px solid #eee;
    }}
    .news-item h3 {{
      font-size: 1.15rem;
      margin-bottom: 1.2rem;
      line-height: 1.4;
    }}
    .news-item h3 a {{
      color: inherit;
      text-decoration: none;
    }}
    .news-item h3 a:hover {{
      text-decoration: underline;
    }}

    /* 三個內容段落 */
    .section {{
      margin-bottom: 1rem;
    }}
    .section-label {{
      font-size: 0.7rem;
      letter-spacing: 0.15em;
      text-transform: uppercase;
      color: #aaa;
      margin-bottom: 0.3rem;
    }}
    .section p {{
      font-size: 0.93rem;
      color: #333;
    }}

    /* 詳細分析特別標示 */
    .section.analysis {{
      background: #f5f5f5;
      border-left: 3px solid #111;
      padding: 1rem 1.2rem;
    }}
    .section.analysis .section-label {{
      color: #666;
    }}
    .section.analysis p {{
      color: #222;
    }}

    /* 深度洞察 */
    .section.key-takeaway {{
      background: #fdf6f0;
      border-left: 3px solid #8B4513;
      padding: 1rem 1.2rem;
      margin-top: 0.5rem;
    }}
    .section.key-takeaway .section-label {{
      color: #8B4513;
    }}
    .section.key-takeaway p {{
      color: #222;
    }}

    /* 深度分析 section */
    .deep-analysis-section .topic-title {{
      color: #8B4513;
    }}
    .deep-badge {{
      font-size: 0.65rem;
      letter-spacing: 0.15em;
      background: #8B4513;
      color: #fff;
      padding: 0.15rem 0.5rem;
      margin-left: 0.8rem;
      vertical-align: middle;
      font-style: normal;
      font-weight: normal;
    }}

    .source {{
      font-size: 0.78rem;
      color: #bbb;
      margin-top: 1rem;
    }}
    .source a {{ color: #bbb; text-decoration: none; }}
    .source a:hover {{ text-decoration: underline; }}

    /* 底部導覽 */
    .nav-bottom {{
      margin-top: 3rem;
      padding-top: 2rem;
      border-top: 1px solid #ddd;
      display: flex;
      justify-content: space-between;
      font-size: 0.9rem;
    }}
    .nav-bottom a {{ color: #111; text-decoration: none; }}
    .nav-bottom a:hover {{ text-decoration: underline; }}

    footer {{
      border-top: 1px solid #ddd;
      text-align: center;
      padding: 2rem;
      font-size: 0.8rem;
      color: #aaa;
      margin-top: 3rem;
    }}
  </style>
</head>
<body>

  <header>
    <h1><a href="../index.html" style="text-decoration:none;color:#111;">Daily Briefing</a></h1>
    <nav>
      <a href="../briefings.html">所有期數</a>
      <a href="#">關於</a>
    </nav>
  </header>

  <div class="article">

    <div class="breadcrumb"><a href="../briefings.html">← 所有期數</a></div>

    <div class="issue-meta">
      <div class="date">{TODAY} · 共 {total_count} 則</div>
      <h2>{data['issue_title']}</h2>
      <p class="summary">{data['issue_summary']}</p>
    </div>

    {overview_html}

    {topics_html}

    <div class="nav-bottom">
      <a href="../briefings.html">← 返回所有期數</a>
    </div>

  </div>

  <footer>© 2026 Daily Briefing · 每個工作日更新</footer>
</body>
</html>"""

    return html

def update_index(data, total_count, market_data=None):
    """
    重寫 index.html 的四個動態區塊（以 DYNAMIC comment 錨點識別）：
      TICKER  — 頂部市場行情快訊
      STATS   — 累計期數與文章數
      LATEST  — 最新一期完整預覽
      RECENT  — 最近 3 期列表
    """
    import re
    import glob as glob_module

    # ── 計算累計數字 ──────────────────────────────────
    all_briefings = sorted(glob_module.glob("briefings/*.html"), reverse=True)
    total_issues  = len(all_briefings)

    # 從最近 20 期抓文章計數加總
    total_articles_sum = total_count
    for fp in all_briefings[1:20]:
        try:
            with open(fp, "r", encoding="utf-8") as fh:
                m = re.search(r'共 (\d+) 則', fh.read())
                if m:
                    total_articles_sum += int(m.group(1))
        except Exception:
            pass

    articles_display = f"{total_articles_sum}+" if total_articles_sum > 10 else str(total_articles_sum)

    # ── TICKER 區塊 ───────────────────────────────────
    if market_data and market_data.get("indices"):
        parts = []
        for idx in market_data["indices"]:
            p = idx["change_pct"]
            arrow = "▲" if p > 0 else ("▼" if p < 0 else "—")
            cls = "ticker-up" if p > 0 else ("ticker-down" if p < 0 else "")
            close_fmt = f"{idx['close']:,.2f}"
            parts.append(
                f'<span class="ticker-item">'
                f'<span class="ticker-label">{idx["name"]}</span>'
                f'{close_fmt} '
                f'<span class="{cls}">{arrow}{abs(p):.2f}%</span>'
                f'</span>'
            )
        date_span = f'<span class="ticker-date">{market_data["as_of"]} 收盤</span>'
        sep = '<span class="ticker-sep">·</span>'
        ticker_content = date_span + sep.join(parts)
        # 複製一份以實現無縫捲動
        ticker_inner = ticker_content + '&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;' + ticker_content
    else:
        ticker_inner = '<span style="color:#555;">市場數據暫不可用</span>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;<span style="color:#555;">市場數據暫不可用</span>'

    new_ticker = (
        f'  <!-- DYNAMIC:TICKER:START -->\n'
        f'  <div class="ticker-bar"><div class="ticker-track">{ticker_inner}</div></div>\n'
        f'  <!-- DYNAMIC:TICKER:END -->'
    )

    # ── STATS 區塊 ────────────────────────────────────
    new_stats = (
        f'        <!-- DYNAMIC:STATS:START -->\n'
        f'        <div class="stats-grid">\n'
        f'          <div class="stat-item"><div class="stat-number">{total_issues}</div><div class="stat-label">期</div></div>\n'
        f'          <div class="stat-item"><div class="stat-number">{articles_display}</div><div class="stat-label">則報導</div></div>\n'
        f'          <div class="stat-item" style="grid-column:span 2;"><div class="stat-number" style="font-size:1.1rem;">每個工作日</div><div class="stat-label">自動更新</div></div>\n'
        f'        </div>\n'
        f'        <!-- DYNAMIC:STATS:END -->'
    )

    # ── LATEST 區塊 ───────────────────────────────────
    overview = data.get("overview", {})
    topic_chips = "".join(
        f'<span class="topic-chip">{t}</span>'
        for t in overview.get("topics_covered", [])
    )
    new_latest = (
        f'      <!-- DYNAMIC:LATEST:START -->\n'
        f'      <div class="latest-issue-wrapper">\n'
        f'        <div class="latest-issue-meta">'
        f'<span class="latest-issue-badge">最新一期</span>'
        f'<span class="latest-issue-date">{TODAY}</span>'
        f'<span class="latest-issue-count">{total_count} 則</span></div>\n'
        f'        <h2 class="latest-issue-title">{data["issue_title"]}</h2>\n'
        f'        <p class="latest-issue-summary">{data["issue_summary"]}</p>\n'
        f'        <div class="latest-issue-topics">{topic_chips}</div>\n'
        f'        <a href="briefings/{TODAY}.html" class="btn-read">閱讀本期全文 →</a>\n'
        f'      </div>\n'
        f'      <!-- DYNAMIC:LATEST:END -->'
    )

    # ── RECENT 區塊（所有期數，跳過本期）────────────
    recent_items_html = ""
    for fp in all_briefings[1:]:
        date_str = os.path.basename(fp).replace(".html", "")
        try:
            with open(fp, "r", encoding="utf-8") as fh:
                fc = fh.read()
            tm = re.search(r'<h2[^>]*>(.*?)</h2>', fc)
            t = tm.group(1) if tm else "—"
        except Exception:
            t = "—"
        recent_items_html += (
            f'        <a href="briefings/{date_str}.html" class="recent-item">\n'
            f'          <div class="recent-date">{date_str}</div>\n'
            f'          <div class="recent-title">{t}</div>\n'
            f'        </a>\n'
        )

    new_recent = (
        f'      <!-- DYNAMIC:RECENT:START -->\n'
        f'      <div class="recent-issues">\n'
        f'        <div class="recent-label">所有期數</div>\n'
        f'{recent_items_html}'
        f'      </div>\n'
        f'      <!-- DYNAMIC:RECENT:END -->'
    )

    # ── 讀取並替換四個動態區塊 ────────────────────────
    with open("index.html", "r", encoding="utf-8") as f:
        content = f.read()

    for anchor, new_block in [
        ("TICKER", new_ticker),
        ("STATS",  new_stats),
        ("LATEST", new_latest),
        ("RECENT", new_recent),
    ]:
        content = re.sub(
            rf'<!-- DYNAMIC:{anchor}:START -->.*?<!-- DYNAMIC:{anchor}:END -->',
            new_block,
            content,
            flags=re.DOTALL,
        )

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(content)
    print(f"   ✓ 首頁更新（{total_issues} 期 / 累計 {total_articles_sum} 則）")


def update_briefings_list():
    """重新生成 briefings.html，掃描所有已存在的期數"""

    import glob

    # 掃描所有已生成的 briefings/*.html
    files = sorted(glob.glob("briefings/*.html"), reverse=True)

    items_html = ""
    for filepath in files:
        filename = os.path.basename(filepath)
        date_str = filename.replace(".html", "")

        # 讀取那個檔案，抓出標題和摘要
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                file_content = f.read()
            import re
            title_match = re.search(r'<h2>(.*?)</h2>', file_content)
            summary_match = re.search(r'<p class="summary">(.*?)</p>', file_content)
            count_match = re.search(r'共 (\d+) 則', file_content)

            title = title_match.group(1) if title_match else "—"
            summary = summary_match.group(1) if summary_match else ""
            count = count_match.group(1) if count_match else "?"
        except:
            title = "—"
            summary = ""
            count = "?"

        items_html += f"""
    <div class="briefing-item">
      <div class="briefing-meta">
        <div class="date">{date_str}</div>
        <div class="count">{count} 則</div>
      </div>
      <div class="briefing-content">
        <h3>{title}</h3>
        <p>{summary}</p>
      </div>
      <div class="briefing-link">
        <a href="briefings/{date_str}.html">→</a>
      </div>
    </div>
"""

    total_issues = len(files)

    briefings_html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>所有期數 · Daily Briefing</title>
{_ga_script()}
  <style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{ font-family: 'Georgia', serif; background: #ffffff; color: #111111; line-height: 1.8; }}
    header {{ border-bottom: 2px solid #111; padding: 2rem 2rem 1.5rem; display: flex; justify-content: space-between; align-items: baseline; }}
    header h1 {{ font-size: 1.2rem; letter-spacing: 0.1em; }}
    header nav a {{ text-decoration: none; color: #111; margin-left: 2rem; font-size: 0.9rem; }}
    header nav a:hover {{ text-decoration: underline; }}
    .page-header {{ max-width: 720px; margin: 3rem auto 2rem; padding: 0 2rem; border-bottom: 1px solid #ddd; padding-bottom: 1.5rem; }}
    .page-header h2 {{ font-size: 1.8rem; margin-bottom: 0.3rem; }}
    .page-header p {{ color: #777; font-size: 0.9rem; }}
    .briefing-list {{ max-width: 720px; margin: 0 auto; padding: 0 2rem; }}
    .briefing-item {{ display: flex; justify-content: space-between; align-items: flex-start; padding: 1.8rem 0; border-bottom: 1px solid #eee; gap: 2rem; }}
    .briefing-item:hover {{ background: #fafafa; }}
    .briefing-meta {{ min-width: 100px; }}
    .briefing-meta .date {{ font-size: 0.8rem; color: #aaa; letter-spacing: 0.05em; }}
    .briefing-meta .count {{ font-size: 0.75rem; color: #bbb; margin-top: 0.3rem; }}
    .briefing-content {{ flex: 1; }}
    .briefing-content h3 {{ font-size: 1.15rem; margin-bottom: 0.4rem; }}
    .briefing-content p {{ font-size: 0.9rem; color: #555; }}
    .briefing-link {{ display: flex; align-items: center; }}
    .briefing-link a {{ color: #111; text-decoration: none; font-size: 1.2rem; font-weight: bold; }}
    .briefing-link a:hover {{ color: #555; }}
    footer {{ border-top: 1px solid #ddd; text-align: center; padding: 2rem; font-size: 0.8rem; color: #aaa; margin-top: 4rem; }}
  </style>
</head>
<body>
  <header>
    <h1><a href="index.html" style="text-decoration:none;color:#111;">Daily Briefing</a></h1>
    <nav>
      <a href="briefings.html">所有期數</a>
      <a href="#">關於</a>
    </nav>
  </header>

  <div class="page-header">
    <h2>所有期數</h2>
    <p>共 {total_issues} 期 · 每個工作日更新</p>
  </div>

  <div class="briefing-list">
    {items_html}
  </div>

  <footer>© 2026 Daily Briefing · 每個工作日更新</footer>
</body>
</html>"""

    with open("briefings.html", "w", encoding="utf-8") as f:
        f.write(briefings_html)


def main():
    if not API_KEY:
        print("❌ 找不到 ANTHROPIC_API_KEY，請先設定環境變數")
        print("   執行：export ANTHROPIC_API_KEY='你的key'")
        return

    print(f"📅 今日日期：{TODAY}")

    print("\n① 抓取市場數據...")
    market_data = fetch_market_data()
    if market_data:
        print(f"   ✓ 已取得 {len(market_data['indices'])} 個指數（截至 {market_data['as_of']}）")
    else:
        print("   ⚠ 市場數據取得失敗，概覽將不顯示指數")

    print("\n② 抓取 RSS 文章...")
    topic_articles = fetch_articles_by_topic()
    deep_articles = fetch_deep_analysis_articles()

    print("\n③ 呼叫 Claude 分析（約 15-30 秒）...")
    data = analyze_with_claude(topic_articles, deep_articles)
    print(f"   ✓ 分析完成，標題：{data['issue_title']}")

    print("\n④ 生成 HTML...")
    total_count = sum(
        len(t["articles"]) for t in data["topics"]
        if t["topic_name"] != "深度分析"
    )
    html = generate_html(data, market_data)

    os.makedirs("briefings", exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"   ✓ 生成：{OUTPUT_PATH}")

    print("\n⑤ 更新首頁與列表頁...")
    update_index(data, total_count, market_data)
    update_briefings_list()
    print("   ✓ 首頁與列表頁已更新")

    print(f"\n✅ 全部完成！")


if __name__ == "__main__":
    main()
