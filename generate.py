import anthropic
import feedparser
import os
import json
from datetime import date

# ── 設定區 ────────────────────────────────────────
# 安全做法：從環境變數讀取，不要直接寫在程式碼裡
# 本機測試時，在終端機執行：export ANTHROPIC_API_KEY="你的key"
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

TODAY = date.today().strftime("%Y-%m-%d")
TODAY_DISPLAY = date.today().strftime("%Y 年 %#m 月 %#d 日")
OUTPUT_PATH = f"briefings/{TODAY}.html"

# ── 三個主題的 RSS 來源 ────────────────────────────
FEEDS = {
    "金融市場（總經）": [
        "https://feeds.reuters.com/reuters/businessNews",
        "https://www.theguardian.com/business/economics/rss",
        "https://www.economist.com/finance-and-economics/rss.xml",
    ],
    "大公司重大新聞（個體）": [
        "https://feeds.reuters.com/reuters/companyNews",
        "https://www.theguardian.com/business/rss",
    ],
    "央行利率決策": [
        "https://feeds.reuters.com/reuters/financialNews",
        "https://www.theguardian.com/business/interest-rates/rss",
    ],
}

ARTICLES_PER_TOPIC = 4   # 每個主題最多選出幾篇
# ──────────────────────────────────────────────────


def fetch_articles_by_topic():
    """依主題抓 RSS，回傳 dict: {主題: [文章列表]}"""
    result = {}
    for topic, urls in FEEDS.items():
        articles = []
        for url in urls:
            try:
                feed = feedparser.parse(url)
                for entry in feed.entries[:6]:
                    articles.append({
                        "title": entry.get("title", ""),
                        "summary": entry.get("summary", "")[:400],
                        "link": entry.get("link", ""),
                        "source": feed.feed.get("title", "Unknown"),
                    })
            except Exception as e:
                print(f"   ⚠ 抓取失敗：{url} ({e})")
        result[topic] = articles
        print(f"   [{topic}] 抓到 {len(articles)} 則原始文章")
    return result


def build_prompt(topic_articles):
    """把三個主題的文章整理成給 Claude 的 prompt"""

    articles_block = ""
    for topic, articles in topic_articles.items():
        articles_block += f"\n## 主題：{topic}\n"
        for i, a in enumerate(articles, 1):
            articles_block += f"{i}. [{a['source']}] {a['title']}\n{a['summary']}\n連結：{a['link']}\n\n"

    prompt = f"""你是一位專業的國際金融市場分析師，負責每日撰寫一份給機構投資人與財經從業人員閱讀的情報簡報。

今天是 {TODAY}，以下是從各大財經媒體抓到的新聞：

{articles_block}

---

請根據以上資料，針對三個主題各選出最重要的 {ARTICLES_PER_TOPIC} 則新聞（不夠的話選有的），
每則新聞必須包含以下三個部分，請用**繁體中文**撰寫：

1. **事件背景介紹**：這件事發生的來龍去脈、相關歷史背景、以及讀者需要知道的前情提要（3-4句）
2. **事件內容及意義**：具體發生了什麼，為什麼重要，對市場或產業的直接影響（3-4句）
3. **詳細分析及研究**：結合歷史數據、市場先例、總體經濟邏輯，深入分析這件事的中長期含義、潛在風險或機會（4-6句，需有數據或歷史案例支撐）

請用以下 JSON 格式回覆，不要有任何說明文字或 markdown 代碼框：

{{
  "issue_title": "今日整體金融市場的核心主題（一句話，例如：聯準會鷹派信號壓制風險資產，美元走強逼近年高）",
  "issue_summary": "今日市場整體局勢的總結（3句話，需涵蓋三個主題的連動關係）",
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
    }}
  ]
}}"""

    return prompt


def analyze_with_claude(topic_articles):
    """呼叫 Claude API 進行分析"""
    client = anthropic.Anthropic(api_key=API_KEY)
    prompt = build_prompt(topic_articles)

    message = client.messages.create(
        model="claude-opus-4-5",
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


def generate_article_html(article):
    """把單篇文章轉成 HTML"""
    return f"""
    <div class="news-item">
      <h3>{article['title']}</h3>

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


def generate_html(data):
    """把完整 JSON 資料轉成 HTML 頁面"""

    topics_html = ""
    total_count = 0

    for topic in data["topics"]:
        articles = topic.get("articles", [])
        total_count += len(articles)

        articles_html = ""
        for a in articles:
            articles_html += generate_article_html(a)

        topics_html += f"""
    <section class="topic-section">
      <div class="topic-header">
        <h2 class="topic-title">{topic['topic_name']}</h2>
      </div>
      {articles_html}
    </section>
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
      margin-bottom: 3rem;
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

    /* 主題區塊 */
    .topic-section {{
      margin-bottom: 4rem;
    }}
    .topic-header {{
      border-bottom: 1px solid #111;
      padding-bottom: 0.5rem;
      margin-bottom: 1.5rem;
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

    {topics_html}

    <div class="nav-bottom">
      <a href="../briefings.html">← 返回所有期數</a>
    </div>

  </div>

  <footer>© 2026 Daily Briefing · 每個工作日更新</footer>
</body>
</html>"""

    return html


def main():
    if not API_KEY:
        print("❌ 找不到 ANTHROPIC_API_KEY，請先設定環境變數")
        print("   執行：export ANTHROPIC_API_KEY='你的key'")
        return

    print(f"📅 今日日期：{TODAY}")

    print("\n① 抓取 RSS 文章...")
    topic_articles = fetch_articles_by_topic()

    print("\n② 呼叫 Claude 分析（這需要約 15-30 秒）...")
    data = analyze_with_claude(topic_articles)
    print(f"   ✓ 分析完成，標題：{data['issue_title']}")

    print("\n③ 生成 HTML...")
    html = generate_html(data)

    os.makedirs("briefings", exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n✅ 完成：{OUTPUT_PATH}")


if __name__ == "__main__":
    main()
