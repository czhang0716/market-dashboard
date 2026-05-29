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
import pandas as pd
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
    """获取所有标的实时报价"""
    cached = cache_get("quotes")
    if cached:
        return cached

    symbols_map = {
        "sp500":  ("^GSPC",   "S&P 500"),
        "nasdaq": ("^IXIC",   "NASDAQ"),
        "sox":    ("^SOX",    "SOX 半导体"),
        "crcl":   ("CRCL",    "Circle (CRCL)"),
        "nbis":   ("NBIS",    "Nebius (NBIS)"),
        "uuuu":   ("UUUU",    "Energy Fuels (UUUU)"),
        "uamy":   ("UAMY",    "US Antimony (UAMY)"),
        "btcusd": ("BTC-USD", "Bitcoin (BTC/USD)"),
    }
    all_symbols = [v[0] for v in symbols_map.values()]

    df = yf.download(all_symbols, period="5d", interval="1d",
                     progress=False, auto_adjust=True, group_by="ticker")

    result = {}
    for key, (symbol, name) in symbols_map.items():
        try:
            # group_by="ticker" 时列结构为 (symbol, field)
            if isinstance(df.columns, pd.MultiIndex):
                prices = df[symbol]["Close"].dropna()
            else:
                prices = df["Close"].dropna()
            if len(prices) < 2:
                raise ValueError(f"{symbol} 数据不足")
            price      = float(prices.iloc[-1])
            prev       = float(prices.iloc[-2])
            change     = round(price - prev, 2)
            change_pct = round((change / prev) * 100, 2)
            result[key] = {"name": name, "price": round(price, 2),
                           "change": change, "change_pct": change_pct}
        except Exception as e:
            print(f"  [quotes] {symbol} 跳过: {e}")

    if not result:
        raise ValueError("所有标的获取失败")

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
    """计算各 EMA 及高亮均线（多选）
    区间规则（高亮该区间的下界均线）：
      股价 >= EMA5             → 不高亮（EMA0~EMA5区间）
      EMA5  > 股价 >= EMA10   → 高亮 EMA5
      EMA10 > 股价 >= EMA15   → 高亮 EMA10
      EMA15 > 股价 >= EMA30   → 高亮 EMA15
      EMA30 > 股价 >= EMA45   → 高亮 EMA30
      EMA45 > 股价 >= EMA60   → 高亮 EMA45
      EMA60 > 股价 >= EMA80   → 高亮 EMA60
      EMA80 > 股价 >= EMA100  → 高亮 EMA80
      EMA100> 股价 >= EMA120  → 高亮 EMA100
      股价 < EMA120            → 高亮 EMA120
    均线交叉时股价可同时落入多个区间，全部高亮。
    """
    periods = [5, 10, 15, 20, 30, 45, 60, 80, 100, 120]

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
        return {"mas": {}, "highlighted": []}

    ema_vals = {p: mas[f"EMA{p}"]["value"] for p in periods if f"EMA{p}" in mas}

    # 区间规则：(上界周期, 下界周期, 高亮周期)
    # 股价在 [min(上界值,下界值), max(上界值,下界值)) 之间 → 高亮 高亮周期
    zone_rules = [
        (5,   10,  5),
        (10,  15,  10),
        (15,  30,  15),
        (30,  45,  30),
        (45,  60,  45),
        (60,  80,  60),
        (80,  100, 80),
        (100, 120, 100),
    ]

    highlighted = set()

    # 股价 < EMA120 → 高亮 EMA120
    v120 = ema_vals.get(120)
    if v120 is not None and price < v120:
        highlighted.add("EMA120")

    # 检查各区间（均线值可能交叉，取实际大小判断）
    for upper_p, lower_p, highlight_p in zone_rules:
        upper_val = ema_vals.get(upper_p)
        lower_val = ema_vals.get(lower_p)
        if upper_val is None or lower_val is None:
            continue
        hi = max(upper_val, lower_val)
        lo = min(upper_val, lower_val)
        if lo <= price < hi:
            highlighted.add(f"EMA{highlight_p}")

    # 将高亮信息写入 mas
    for name in mas:
        mas[name]["highlighted"] = name in highlighted

    return {
        "mas": mas,
        "highlighted": sorted(highlighted, key=lambda x: int(x[3:])),
    }


def get_ma_data():
    """获取标普500、纳斯达克、CRCL、NBIS 均线数据"""
    cached = cache_get("ma_data")
    if cached:
        return cached

    quotes        = get_quotes()
    sp500_closes  = fetch_closes("^GSPC")
    nasdaq_closes = fetch_closes("^IXIC")
    sox_closes    = fetch_closes("^SOX")
    crcl_closes   = fetch_closes("CRCL")
    nbis_closes   = fetch_closes("NBIS")
    uuuu_closes   = fetch_closes("UUUU")
    uamy_closes   = fetch_closes("UAMY")
    btc_closes    = fetch_closes("BTC-USD")

    result = {
        "sp500":  calc_mas(sp500_closes,  quotes["sp500"]["price"]),
        "nasdaq": calc_mas(nasdaq_closes, quotes["nasdaq"]["price"]),
        "sox":    calc_mas(sox_closes,    quotes["sox"]["price"]),
        "crcl":   calc_mas(crcl_closes,   quotes["crcl"]["price"]),
        "nbis":   calc_mas(nbis_closes,   quotes["nbis"]["price"]),
        "uuuu":   calc_mas(uuuu_closes,   quotes["uuuu"]["price"]),
        "uamy":   calc_mas(uamy_closes,   quotes["uamy"]["price"]),
        "btcusd": calc_mas(btc_closes,    quotes["btcusd"]["price"]),
    }
    cache_set("ma_data", result, ttl=900)
    return result


# ── 新闻数据（富途 via Google News RSS）────────────────────────────────────

import xml.etree.ElementTree as ET

def get_news(count=10):
    """从 Google News 抓取 futunn.com 美股新闻，缓存 5 分钟"""
    cached = cache_get("news")
    if cached:
        return cached

    # 搜索富途网站上的美股相关新闻
    rss_url = (
        "https://news.google.com/rss/search"
        "?q=site%3Afutunn.com+%E7%BE%8E%E8%82%A1"
        "&hl=zh-CN&gl=CN&ceid=CN%3Azh-Hans"
    )
    req = urllib.request.Request(rss_url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0.0.0 Safari/537.36"
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        xml_text = resp.read().decode("utf-8", errors="ignore")

    root  = ET.fromstring(xml_text)
    items = root.findall("./channel/item")
    news  = []
    for item in items[:count]:
        title = (item.findtext("title") or "").strip()
        link  = (item.findtext("link")  or "").strip()
        pub   = (item.findtext("pubDate") or "").strip()
        if not title or not link:
            continue
        # pubDate 格式：Thu, 29 May 2026 03:00:00 GMT
        ts = 0
        try:
            from email.utils import parsedate_to_datetime
            ts = int(parsedate_to_datetime(pub).timestamp())
        except Exception:
            pass
        news.append({
            "title":  title,
            "url":    link,
            "intro":  "",
            "time":   ts,
            "source": "富途牛牛",
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

