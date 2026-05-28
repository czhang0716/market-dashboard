#!/usr/bin/env python3
"""
市场行情看板 - 本地/云端后端服务器
运行方式：python3 server.py
访问地址：http://localhost:8888
"""

import json
import os
import ssl
import time
import urllib.request
import yfinance as yf
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(os.environ.get("PORT", 8888))

# ── 简单内存缓存 ──────────────────────────────────────────────────────────
_cache = {}

def cache_get(key):
    entry = _cache.get(key)
    if entry and time.time() - entry["ts"] < entry["ttl"]:
        return entry["data"]
    return None

def cache_set(key, data, ttl=60):
    _cache[key] = {"data": data, "ts": time.time(), "ttl": ttl}


# ── 行情数据（yfinance）────────────────────────────────────────────────────

def get_quotes():
    """获取标普500、纳斯达克、CRCL、NBIS 实时报价"""
    cached = cache_get("quotes")
    if cached:
        return cached

    tickers = yf.download(
        ["^GSPC", "^IXIC", "CRCL", "NBIS", "UUUU", "UAMY"],
        period="2d",
        interval="1d",
        progress=False,
        auto_adjust=True,
    )
    closes = tickers["Close"]

    def build_quote(symbol, name):
        prices = closes[symbol].dropna()
        if len(prices) < 2:
            raise ValueError(f"{symbol} 数据不足")
        price      = float(prices.iloc[-1])
        prev       = float(prices.iloc[-2])
        change     = round(price - prev, 2)
        change_pct = round((change / prev) * 100, 2)
        return {"name": name, "price": round(price, 2),
                "change": change, "change_pct": change_pct}

    result = {
        "sp500":  build_quote("^GSPC", "S&P 500"),
        "nasdaq": build_quote("^IXIC", "NASDAQ"),
        "crcl":   build_quote("CRCL",  "Circle (CRCL)"),
        "nbis":   build_quote("NBIS",  "Nebius (NBIS)"),
        "uuuu":   build_quote("UUUU",  "Energy Fuels (UUUU)"),
        "uamy":   build_quote("UAMY",  "US Antimony (UAMY)"),
    }
    cache_set("quotes", result, ttl=60)
    return result


# ── 均线数据（yfinance）────────────────────────────────────────────────────

def fetch_closes(ticker_symbol, days=130):
    """抓取最近 N 天收盘价，缓存 15 分钟"""
    cache_key = f"closes_{ticker_symbol}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    hist   = yf.Ticker(ticker_symbol).history(period="8mo")
    closes = hist["Close"].dropna().tolist()
    result = closes[-days:]
    cache_set(cache_key, result, ttl=900)
    return result


def calc_ema(closes, period):
    """计算 EMA（指数移动平均）"""
    if len(closes) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period  # 用前 N 天 SMA 作为初始值
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return ema


def calc_mas(closes, price):
    """计算各 EMA 及高亮均线
    高亮规则：找到股价刚刚跌破的那条均线
      价格在 EMA5  以上          → 高亮 EMA5
      价格在 EMA5  ~ EMA10 之间  → 高亮 EMA5
      价格在 EMA10 ~ EMA15 之间  → 高亮 EMA10
      价格在 EMA15 ~ EMA30 之间  → 高亮 EMA15
      价格在 EMA30 ~ EMA45 之间  → 高亮 EMA30
      价格在 EMA45 ~ EMA60 之间  → 高亮 EMA45
      价格在 EMA60 ~ EMA80 之间  → 高亮 EMA60
      价格在 EMA80 ~ EMA100 之间 → 高亮 EMA80
      价格在 EMA100~ EMA120 之间 → 高亮 EMA100
      价格在 EMA120 以下         → 高亮 EMA120
    """
    periods = [5, 10, 15, 20, 30, 45, 60, 80, 100, 120]
    # 高亮边界对：(下界周期, 上界周期) → 高亮下界
    # 即：价格在 lower_ema 和 upper_ema 之间时，高亮 lower_ema
    highlight_rules = [
        (5,   10),
        (10,  15),
        (15,  30),
        (30,  45),
        (45,  60),
        (60,  80),
        (80,  100),
        (100, 120),
    ]

    mas = {}
    for p in periods:
        ema_val = calc_ema(closes, p)
        if ema_val is not None:
            diff_pct = (price - ema_val) / ema_val * 100
            mas[f"EMA{p}"] = {
                "value":    round(ema_val, 2),
                "diff":     round(price - ema_val, 2),
                "diff_pct": round(diff_pct, 2),
            }
    if not mas:
        return {"mas": {}, "nearest": None}

    # 确定高亮均线
    highlight_name = None
    ema_vals = {p: mas[f"EMA{p}"]["value"] for p in periods if f"EMA{p}" in mas}

    if ema_vals:
        ema5_val = ema_vals.get(5)
        ema120_val = ema_vals.get(120)

        if ema5_val is not None and price >= ema5_val:
            # 价格在 EMA5 以上，高亮 EMA5
            highlight_name = "EMA5"
        elif ema120_val is not None and price < ema120_val:
            # 价格在 EMA120 以下，高亮 EMA120
            highlight_name = "EMA120"
        else:
            # 找到价格所在的区间
            for lower_p, upper_p in highlight_rules:
                lower_val = ema_vals.get(lower_p)
                upper_val = ema_vals.get(upper_p)
                if lower_val is not None and upper_val is not None:
                    if lower_val > price >= upper_val:
                        highlight_name = f"EMA{lower_p}"
                        break
            # 若未匹配（均线数据不全），退回最近均线
            if highlight_name is None:
                nearest = min(mas.items(), key=lambda x: abs(x[1]["diff_pct"]))
                highlight_name = nearest[0]

    nearest_data = mas.get(highlight_name, list(mas.values())[0])
    return {
        "mas": mas,
        "nearest": {
            "name":     highlight_name,
            "value":    nearest_data["value"],
            "diff":     nearest_data["diff"],
            "diff_pct": nearest_data["diff_pct"],
        }
    }


def get_ma_data():
    """获取标普500、纳斯达克、CRCL、NBIS 均线数据"""
    cached = cache_get("ma_data")
    if cached:
        return cached

    quotes        = get_quotes()
    sp500_closes  = fetch_closes("^GSPC")
    nasdaq_closes = fetch_closes("^IXIC")
    crcl_closes   = fetch_closes("CRCL")
    nbis_closes   = fetch_closes("NBIS")
    uuuu_closes   = fetch_closes("UUUU")
    uamy_closes   = fetch_closes("UAMY")

    result = {
        "sp500":  calc_mas(sp500_closes,  quotes["sp500"]["price"]),
        "nasdaq": calc_mas(nasdaq_closes, quotes["nasdaq"]["price"]),
        "crcl":   calc_mas(crcl_closes,   quotes["crcl"]["price"]),
        "nbis":   calc_mas(nbis_closes,   quotes["nbis"]["price"]),
        "uuuu":   calc_mas(uuuu_closes,   quotes["uuuu"]["price"]),
        "uamy":   calc_mas(uamy_closes,   quotes["uamy"]["price"]),
    }
    cache_set("ma_data", result, ttl=900)
    return result


# ── 新闻数据（新浪财经）─────────────────────────────────────────────────────

def get_news(count=10):
    """从新浪财经抓取美股新闻，缓存 5 分钟"""
    cached = cache_get("news")
    if cached:
        return cached

    url = f"https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2516&num={count}&page=1"
    ctx = ssl._create_unverified_context()
    req = urllib.request.Request(url, headers={
        "Referer":    "https://finance.sina.com.cn",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0.0.0 Safari/537.36",
    })
    with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    items = data.get("result", {}).get("data", [])
    news  = []
    for item in items:
        news.append({
            "title":  item.get("title", "").strip(),
            "url":    item.get("url",   "").strip(),
            "intro":  item.get("intro", "").strip(),
            "time":   item.get("ctime", 0),
            "source": item.get("media_name", "新浪财经").strip(),
        })

    cache_set("news", news, ttl=300)
    return news


# ── HTTP 服务器 ───────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        print(f"  {self.address_string()} {args[0]}")

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type",   "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, content_type):
        try:
            with open(path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type",   content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            self.send_json({"ok": True})

        elif self.path == "/api/quotes":
            try:
                self.send_json({"ok": True, "data": get_quotes()})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, status=502)

        elif self.path == "/api/ma":
            try:
                self.send_json({"ok": True, "data": get_ma_data()})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, status=502)

        elif self.path == "/api/news":
            try:
                self.send_json({"ok": True, "data": get_news()})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, status=502)

        elif self.path in ("/", "/index.html"):
            self.send_file("index.html", "text/html; charset=utf-8")

        else:
            self.send_response(404)
            self.end_headers()


# ── 入口 ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"\n✅ 市场行情看板已启动")
    print(f"   访问地址：http://localhost:{PORT}")
    print(f"   按 Ctrl+C 停止\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")

