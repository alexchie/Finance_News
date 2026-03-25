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
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

TODAY = date.today().strftime("%Y-%m-%d")
TODAY_DISPLAY = date.today().strftime("%Y 年 %#m 月 %#d 日")
OUTPUT_PATH = f"briefings/{TODAY}.html"

TODAY_CUTOFF = time.time() - 86400  # 24小時前的 POSIX timestamp
SCAN_PER_FEED = 15                   # 每個 feed 最多掃描的條數（過濾後數量會減少）

# ── RSS 來源 ───────────────────────────────────────
FEEDS = {
    "金融市場（總經）": [
        "https://feeds.reuters.com/reuters/businessNews",
        "https://www.theguardian.com/business/economics/rss",
        "https://www.economist.com/finance-and-economics/rss.xml",
        "https://feeds.bbci.co.uk/news/business/rss.xml",
        "https://www.cnbc.com/id/10000664/device/rss/rss.html",
    ],
    "大公司重大新聞（個體）": [
        "https://feeds.reuters.com/reuters/companyNews",
        "https://www.theguardian.com/business/rss",
        "https://feeds.bbci.co.uk/news/business/rss.xml",
        "https://www.cnbc.com/id/10001147/device/rss/rss.html",
        "https://feeds.marketwatch.com/marketwatch/topstories/",
    ],
    "央行利率決策": [
        "https://feeds.reuters.com/reuters/financialNews",
        "https://www.theguardian.com/business/interest-rates/rss",
        "https://feeds.bbci.co.uk/news/business/economy/rss.xml",
        "https://feeds.marketwatch.com/marketwatch/realtimeheadlines/",
    ],
    # 深度分析：The Economist 長文，不套用24小時過濾（The Economist 為週刊）
    "深度分析": [
        "https://www.economist.com/leaders/rss.xml",
        "https://www.economist.com/briefing/rss.xml",
    ],
}

ARTICLES_PER_TOPIC = 4   # 每個主題最多選出幾篇
# ──────────────────────────────────────────────────


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
                        "summary": entry.get("summary", "")[:400],
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
                    "summary": entry.get("summary", "")[:600],
                    "link": entry.get("link", ""),
                    "source": feed.feed.get("title", "The Economist"),
                })
        except Exception as e:
            print(f"   ⚠ 深度分析抓取失敗：{url} ({e})")
    print(f"   [深度分析] 抓到 {len(articles)} 則候選")
    return articles


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

    prompt = f"""你是一位專業的國際金融市場分析師，負責每日撰寫一份給機構投資人與財經從業人員閱讀的情報簡報。

今天是 {TODAY}，以下是從各大財經媒體抓到的新聞（三大主題僅含過去24小時內發布的報導）：

{articles_block}
{deep_block}
---

請完成以下工作，全部用**繁體中文**撰寫：

**一、三大主題新聞分析**
針對三個主題各選出最重要的 {ARTICLES_PER_TOPIC} 則新聞（不夠的話選有的），每則包含：
1. **事件背景介紹**：這件事發生的來龍去脈、相關歷史背景（3-4句）
2. **事件內容及意義**：具體發生了什麼，為什麼重要，對市場的直接影響（3-4句）
3. **詳細分析及研究**：結合歷史數據、市場先例、總體經濟邏輯，深入分析中長期含義（4-6句，需有數據或歷史案例支撐）

**二、今日概覽**
- overview.topics_covered：每個主題（金融市場、大公司、央行）各用5-10字的短語概括今日核心動向
- overview.regions：列出今日新聞涉及的主要地理區域（如美國、歐盟、日本、中國等）

**三、深度分析**
從 The Economist 候選文章中選出最具分析深度的一篇（不限24小時），完成：
1. **事件背景介紹**（3-4句）
2. **事件內容及意義**（3-4句）
3. **詳細分析及研究**（4-6句）
4. **key_takeaway**：針對此議題的深度洞見，結合歷史視野、制度邏輯、中長期結構性含義（**6-8句**，這是本篇的核心價值，請務必深入）

請用以下 JSON 格式回覆，不要有任何說明文字或 markdown 代碼框：

{{
  "issue_title": "今日整體金融市場的核心主題（一句話）",
  "issue_summary": "今日市場整體局勢的總結（3句話，涵蓋三個主題的連動關係）",
  "overview": {{
    "topics_covered": ["金融市場今日核心短語", "大公司今日核心短語", "央行今日核心短語"],
    "regions": ["美國", "歐盟"]
  }},
  "topics": [
    {{
      "topic_name": "金融市場（總經）",
      "articles": [
        {{
          "title": "新聞繁體中文標題",
          "background": "事件背景介紹內容",
          "content": "事件內容及意義內容",
          "analysis": "詳細分析及研究內容",
          "source_name": "來源媒體名稱",
          "source_url": "原始連結"
        }}
      ]
    }},
    {{
      "topic_name": "大公司重大新聞（個體）",
      "articles": []
    }},
    {{
      "topic_name": "央行利率決策",
      "articles": []
    }},
    {{
      "topic_name": "深度分析",
      "articles": [
        {{
          "title": "文章繁體中文標題",
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
        model="claude-opus-4-5",
        max_tokens=8192,
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


def generate_article_html(article):
    """把單篇文章轉成 HTML（標題為可點擊連結）"""
    return f"""
    <div class="news-item">
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


def generate_deep_article_html(article):
    """把深度分析文章轉成 HTML（多一個 key_takeaway 欄位）"""
    return f"""
    <div class="news-item deep-item">
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


def generate_html(data):
    """把完整 JSON 資料轉成 HTML 頁面"""

    # ── 各 topic 對應的 anchor ID ──────────────────────
    SECTION_IDS = {
        "金融市場（總經）": "section-macro",
        "大公司重大新聞（個體）": "section-corporate",
        "央行利率決策": "section-central",
        "深度分析": "section-deep",
    }

    # ── 生成各 topic 的 HTML ───────────────────────────
    topics_html = ""
    total_count = 0

    for topic in data["topics"]:
        topic_name = topic["topic_name"]
        articles = topic.get("articles", [])
        is_deep = (topic_name == "深度分析")

        if not is_deep:
            total_count += len(articles)

        articles_html = ""
        for a in articles:
            if is_deep:
                articles_html += generate_deep_article_html(a)
            else:
                articles_html += generate_article_html(a)

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
    regions = overview.get("regions", [])
    topics_covered = overview.get("topics_covered", [])

    regions_html = "".join(f'<span class="region-tag">{r}</span>' for r in regions)

    toc_topic_names = ["金融市場（總經）", "大公司重大新聞（個體）", "央行利率決策", "深度分析"]
    toc_rows_html = ""
    for i, tname in enumerate(toc_topic_names):
        anchor = SECTION_IDS.get(tname, "#")
        is_deep_row = (tname == "深度分析")
        row_class = " deep" if is_deep_row else ""
        theme = topics_covered[i] if i < len(topics_covered) else ""
        toc_rows_html += f"""
        <div class="toc-row{row_class}">
          <a href="#{anchor}" class="toc-label">{tname}</a>
          <span class="toc-theme">{theme}</span>
        </div>"""

    overview_html = f"""
    <div class="overview-section">
      <div class="overview-label">今日概覽</div>
      <div class="overview-regions">{regions_html}</div>
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
    .overview-regions {{
      display: flex; flex-wrap: wrap; gap: 0.5rem;
      margin-bottom: 1.2rem;
    }}
    .region-tag {{
      background: #111; color: #fff;
      font-size: 0.72rem; padding: 0.2rem 0.6rem;
      letter-spacing: 0.05em;
    }}
    .overview-toc {{ display: flex; flex-direction: column; gap: 0.6rem; }}
    .toc-row {{ display: flex; align-items: baseline; gap: 0.8rem; }}
    .toc-label {{
      font-size: 0.75rem; font-weight: bold;
      color: #111; text-decoration: none;
      min-width: 165px; flex-shrink: 0;
    }}
    .toc-label:hover {{ text-decoration: underline; }}
    .toc-theme {{ font-size: 0.88rem; color: #555; }}
    .toc-row.deep .toc-label {{ color: #8B4513; }}

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

def update_index(data, total_count):
    """更新首頁的最新一期預覽"""

    # 讀取現有的 index.html
    with open("index.html", "r", encoding="utf-8") as f:
        content = f.read()

    # 找到 .latest 區塊，整個替換
    import re
    new_latest = f"""  <!-- 最新一期預覽 -->
  <section class="latest">
    <p class="tag">最新一期 · {TODAY}</p>
    <h3>{data['issue_title']}</h3>
    <p>{data['issue_summary']}</p>
    <a href="briefings/{TODAY}.html">閱讀本期 →</a>
  </section>"""

    content = re.sub(
        r'<!-- 最新一期預覽 -->.*?</section>',
        new_latest,
        content,
        flags=re.DOTALL
    )

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(content)


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

    print("\n① 抓取 RSS 文章...")
    topic_articles = fetch_articles_by_topic()
    deep_articles = fetch_deep_analysis_articles()

    print("\n② 呼叫 Claude 分析（這需要約 15-30 秒）...")
    data = analyze_with_claude(topic_articles, deep_articles)
    print(f"   ✓ 分析完成，標題：{data['issue_title']}")

    print("\n③ 生成 HTML...")
    html = generate_html(data)

    os.makedirs("briefings", exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"   ✓ 生成：{OUTPUT_PATH}")

    print("\n④ 更新首頁與列表頁...")
    update_index(data, total_count=len(data["articles"]) if "articles" in data else sum(len(t["articles"]) for t in data["topics"]))
    update_briefings_list()
    print("   ✓ 首頁與列表頁已更新")

    print(f"\n✅ 全部完成！")


if __name__ == "__main__":
    main()
