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
        "gold":   ("GC=F",    "黄金 Gold"),
        "googl":  ("GOOGL",   "Alphabet (GOOGL)"),
        "mu":     ("MU",      "美光科技 (MU)"),
        "nvda":   ("NVDA",    "英伟达 (NVDA)"),
    }

    # 加载自定义卡片
    config = load_cards_config()
    if "custom" in config:
        for card_id, info in config["custom"].items():
            symbols_map[card_id] = (info["symbol"], info["name"])

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

    # 默认股票
    default_symbols = {
        "sp500": "^GSPC",
        "nasdaq": "^IXIC",
        "sox": "^SOX",
        "crcl": "CRCL",
        "nbis": "NBIS",
        "uuuu": "UUUU",
        "uamy": "UAMY",
        "btcusd": "BTC-USD",
        "gold": "GC=F",
        "googl": "GOOGL",
        "mu": "MU",
        "nvda": "NVDA",
    }

    result = {}

    # 处理默认股票
    for key, symbol in default_symbols.items():
        if key in quotes:
            closes = fetch_closes(symbol)
            result[key] = calc_mas(closes, quotes[key]["price"])

    # 处理自定义股票
    config = load_cards_config()
    if "custom" in config:
        for card_id, info in config["custom"].items():
            if card_id in quotes:
                closes = fetch_closes(info["symbol"])
                result[card_id] = calc_mas(closes, quotes[card_id]["price"])

    cache_set("ma_data", result, ttl=900)
    return result


# ── 新闻数据（富途 via Google News RSS）────────────────────────────────────

import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
import random

# 过滤中国/A股/中概股相关新闻
_CN_FILTER_KEYWORDS = [
    "中国", "A股", "中概", "港股", "沪市", "深市", "沪深",
    "上证", "深证", "创业板", "科创板", "北交所", "新三板",
    "人民币", "央行", "证监会", "国内",
]

def _is_cn_news(title: str) -> bool:
    return any(kw in title for kw in _CN_FILTER_KEYWORDS)

# 多个 User-Agent 轮换，降低被封概率
_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Feedfetcher-Google; (+http://www.google.com/feedfetcher.html)",
]

def _random_ua() -> str:
    return random.choice(_USER_AGENTS)


def _fetch_rss(url: str, count: int, source: str, filter_cn: bool = True) -> list:
    """通用 RSS 抓取，返回最多 count 条，可选过滤中文 A 股新闻"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _random_ua()})
        with urllib.request.urlopen(req, timeout=10) as resp:
            xml_text = resp.read().decode("utf-8", errors="ignore")
        root  = ET.fromstring(xml_text)
        items = root.findall("./channel/item")
        news  = []
        for item in items:
            if len(news) >= count:
                break
            title = (item.findtext("title") or "").strip()
            link  = (item.findtext("link")  or "").strip()
            pub   = (item.findtext("pubDate") or "").strip()
            if not title or not link:
                continue
            if filter_cn and _is_cn_news(title):
                continue
            ts = 0
            try:
                ts = int(parsedate_to_datetime(pub).timestamp())
            except Exception:
                pass
            news.append({"title": title, "url": link, "time": ts, "source": source})
        return news
    except Exception as e:
        print(f"  [rss] {url[:60]}... 失败: {e}")
        return []


# ── 卡片新闻：Google News RSS（富途）→ Bing News RSS（中文）→ 空列表 ──────

_CARD_NEWS_GOOGLE = {
    "sp500":  "site%3Afutunn.com+%E6%A0%87%E6%99%AE500",
    "nasdaq": "site%3Afutunn.com+%E7%BA%B3%E6%8C%87",
    "sox":    "site%3Afutunn.com+%E8%B4%B9%E5%9F%8E%E5%8D%8A%E5%AF%BC%E4%BD%93",
    "nvda":   "site%3Afutunn.com+%E8%8B%B1%E4%BC%9F%E8%BE%BE+NVDA",
    "mu":     "site%3Afutunn.com+%E7%BE%8E%E5%85%89%E7%A7%91%E6%8A%80",
    "nbis":   "site%3Afutunn.com+Nebius",
    "googl":  "site%3Afutunn.com+Alphabet+GOOGL",
    "crcl":   "site%3Afutunn.com+Circle+CRCL",
    "uuuu":   "site%3Afutunn.com+Energy+Fuels+UUUU",
    "uamy":   "site%3Afutunn.com+Antimony+UAMY",
    "btcusd": "site%3Afutunn.com+%E6%AF%94%E7%89%B9%E5%B8%81",
    "gold":   "site%3Afutunn.com+%E9%BB%84%E9%87%91",
}

_CARD_NEWS_BING = {
    "sp500":  "%E6%A0%87%E6%99%AE500+%E7%BE%8E%E8%82%A1",
    "nasdaq": "%E7%BA%B3%E6%8C%87+%E7%BE%8E%E8%82%A1",
    "sox":    "%E8%B4%B9%E5%9F%8E%E5%8D%8A%E5%AF%BC%E4%BD%93",
    "nvda":   "%E8%8B%B1%E4%BC%9F%E8%BE%BE+NVDA",
    "mu":     "%E7%BE%8E%E5%85%89%E7%A7%91%E6%8A%80+MU",
    "nbis":   "Nebius+NBIS",
    "googl":  "Alphabet+GOOGL",
    "crcl":   "Circle+CRCL",
    "uuuu":   "Energy+Fuels+UUUU",
    "uamy":   "Antimony+UAMY",
    "btcusd": "%E6%AF%94%E7%89%B9%E5%B8%81+BTC",
    "gold":   "%E9%BB%84%E9%87%91+%E7%BE%8E%E8%82%A1",
}

_CARD_NEWS_YAHOO = {
    "sp500":  "%5EGSPC",
    "nasdaq": "%5EIXIC",
    "sox":    "%5ESOX",
    "nvda":   "NVDA",
    "mu":     "MU",
    "nbis":   "NBIS",
    "googl":  "GOOGL",
    "crcl":   "CRCL",
    "uuuu":   "UUUU",
    "uamy":   "UAMY",
    "btcusd": "BTC-USD",
    "gold":   "GC%3DF",
}

# 磁盘持久化缓存（新闻抓取失败时回退，但仍限制在3天内）
import json as _json
from pathlib import Path as _Path
_NEWS_DISK_CACHE = _Path("news_cache.json")
_CARD_NEWS_DISK = _Path("card_news_cache.json")
_NEWS_MAX_AGE_SECONDS = 3 * 24 * 60 * 60


def _save_disk(path: _Path, data) -> None:
    try:
        path.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

def _load_disk(path: _Path):
    try:
        if path.exists():
            return _json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def _is_recent_ts(ts: int) -> bool:
    return bool(ts) and (time.time() - ts <= _NEWS_MAX_AGE_SECONDS)


def _filter_recent_news(items: list) -> list:
    return [it for it in items if _is_recent_ts(int(it.get("time") or 0))]


def _dedupe_news(items: list) -> list:
    seen = set()
    result = []
    for it in items:
        key = ((it.get("url") or "").strip(), (it.get("title") or "").strip())
        if not key[0] and not key[1]:
            continue
        if key in seen:
            continue
        seen.add(key)
        result.append(it)
    return result


def _fetch_yahoo_news(ticker: str, count: int) -> list:
    url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
    return _fetch_rss(url, count, "Yahoo Finance", filter_cn=False)


def _fetch_card_news_one(key: str, count: int) -> list:
    """先试 Google News RSS（富途），失败再试 Bing，最后用 Yahoo 英文补位。"""
    zh_news = []

    g_query = _CARD_NEWS_GOOGLE.get(key, "")
    if g_query:
        url = (f"https://news.google.com/rss/search?q={g_query}"
               "&hl=zh-CN&gl=CN&ceid=CN%3Azh-Hans")
        zh_news.extend(_filter_recent_news(_fetch_rss(url, count * 3, "富途牛牛", filter_cn=True)))

    b_query = _CARD_NEWS_BING.get(key, "")
    if b_query and len(_dedupe_news(zh_news)) < count:
        url = f"https://www.bing.com/news/search?q={b_query}&format=rss&setlang=zh-CN"
        zh_news.extend(_filter_recent_news(_fetch_rss(url, count * 3, "Bing新闻", filter_cn=True)))

    zh_news = _dedupe_news(zh_news)[:count]
    if len(zh_news) >= count:
        return zh_news

    ticker = _CARD_NEWS_YAHOO.get(key, "")
    en_news = []
    if ticker:
        en_news = _filter_recent_news(_fetch_yahoo_news(ticker, count * 3))

    return _dedupe_news(zh_news + en_news)[:count]



def get_news(count: int = 10) -> list:
    """底部大盘新闻：只保留3天内中文新闻，不强行凑满。"""
    cached = cache_get("news")
    if cached:
        return cached

    url = ("https://news.google.com/rss/search"
           "?q=site%3Afutunn.com+%E7%BE%8E%E8%82%A1"
           "&hl=zh-CN&gl=CN&ceid=CN%3Azh-Hans")
    news = _filter_recent_news(_fetch_rss(url, count * 3, "富途牛牛", filter_cn=True))

    if len(news) < 3:
        url2 = "https://www.bing.com/news/search?q=%E7%BE%8E%E8%82%A1+%E8%A1%8C%E6%83%85&format=rss&setlang=zh-CN"
        news = _dedupe_news(news + _filter_recent_news(_fetch_rss(url2, count * 3, "Bing新闻", filter_cn=True)))

    if len(news) < 3:
        disk = _filter_recent_news(_load_disk(_NEWS_DISK_CACHE) or [])
        news = _dedupe_news(news + disk)

    news = news[:count]
    if news:
        _save_disk(_NEWS_DISK_CACHE, news)
        cache_set("news", news, ttl=300)
    return news


def get_card_news() -> dict:
    """卡片新闻：3天内中文优先，不足再用3天内英文补位，不强行凑满。"""
    cached = cache_get("card_news")
    if cached:
        return cached

    result = {}

    # 默认股票新闻
    for key in _CARD_NEWS_GOOGLE:
        result[key] = _fetch_card_news_one(key, count=3)

    # 自定义股票新闻（用 Yahoo RSS）
    config = load_cards_config()
    if "custom" in config:
        for card_id, info in config["custom"].items():
            symbol = info["symbol"]
            news = _filter_recent_news(_fetch_yahoo_news(symbol, count=3))
            result[card_id] = news

    disk = _load_disk(_CARD_NEWS_DISK) or {}
    for key in result:
        if len(result[key]) >= 3:
            continue
        recent_disk = _filter_recent_news(disk.get(key, [])) if isinstance(disk.get(key, []), list) else []
        result[key] = _dedupe_news(result[key] + recent_disk)[:3]

    _save_disk(_CARD_NEWS_DISK, result)
    cache_set("card_news", result, ttl=600)
    return result


# ── 卡片配置管理 ──────────────────────────────────────────────────────────

_CARDS_CONFIG_FILE = _Path("cards_config.json")
_DEFAULT_CARDS_ORDER = [
    "sp500", "nasdaq", "sox", "nvda", "mu", "nbis",
    "googl", "crcl", "uuuu", "uamy", "btcusd", "gold"
]

# 密码（生产环境应该用环境变量）
_ADMIN_PASSWORD = "czhang95"

def load_cards_config():
    """加载卡片配置"""
    config = _load_disk(_CARDS_CONFIG_FILE)
    if not config:
        config = {"order": _DEFAULT_CARDS_ORDER, "deleted": []}
    return config

def save_cards_config(config):
    """保存卡片配置"""
    _save_disk(_CARDS_CONFIG_FILE, config)

def verify_password(password):
    """验证密码"""
    return password == _ADMIN_PASSWORD


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
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
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

        elif self.path == "/api/card-news":
            try:
                self.send_json({"ok": True, "data": get_card_news()})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, status=502)

        elif self.path == "/api/cards-config":
            try:
                config = load_cards_config()
                self.send_json({"ok": True, "data": config})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, status=502)

        elif self.path in ("/", "/index.html"):
            self.send_file("index.html", "text/html; charset=utf-8")

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode('utf-8')

        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self.send_json({"ok": False, "error": "Invalid JSON"}, status=400)
            return

        if self.path == "/api/login":
            password = data.get("password", "")
            if verify_password(password):
                self.send_json({"ok": True, "token": "admin-token"})
            else:
                self.send_json({"ok": False, "error": "Invalid password"}, status=401)

        elif self.path == "/api/card-move":
            password = data.get("password", "")
            if not verify_password(password):
                self.send_json({"ok": False, "error": "Unauthorized"}, status=401)
                return

            try:
                config = load_cards_config()
                card_id = data.get("cardId")
                direction = data.get("direction")  # "up" or "down"

                if card_id not in config["order"]:
                    self.send_json({"ok": False, "error": "Card not found"}, status=404)
                    return

                idx = config["order"].index(card_id)
                if direction == "up" and idx > 0:
                    config["order"][idx], config["order"][idx-1] = config["order"][idx-1], config["order"][idx]
                elif direction == "down" and idx < len(config["order"]) - 1:
                    config["order"][idx], config["order"][idx+1] = config["order"][idx+1], config["order"][idx]

                save_cards_config(config)
                self.send_json({"ok": True, "data": config})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, status=500)

        elif self.path == "/api/card-delete":
            password = data.get("password", "")
            if not verify_password(password):
                self.send_json({"ok": False, "error": "Unauthorized"}, status=401)
                return

            try:
                config = load_cards_config()
                card_id = data.get("cardId")

                if card_id in config["order"]:
                    config["order"].remove(card_id)
                    if card_id not in config["deleted"]:
                        config["deleted"].append(card_id)

                save_cards_config(config)
                self.send_json({"ok": True, "data": config})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, status=500)

        elif self.path == "/api/card-add":
            password = data.get("password", "")
            if not verify_password(password):
                self.send_json({"ok": False, "error": "Unauthorized"}, status=401)
                return

            try:
                config = load_cards_config()
                symbol = data.get("symbol", "").strip().upper()
                name = data.get("name", "").strip()

                if not symbol or not name:
                    self.send_json({"ok": False, "error": "Missing symbol or name"}, status=400)
                    return

                # 生成卡片 ID（小写，去掉特殊字符）
                card_id = symbol.lower().replace("-", "").replace("^", "").replace("=", "")

                if card_id in config["order"]:
                    self.send_json({"ok": False, "error": "Card already exists"}, status=400)
                    return

                # 添加到配置
                config["order"].append(card_id)

                # 保存映射关系（用于后续获取数据）
                if "custom" not in config:
                    config["custom"] = {}
                config["custom"][card_id] = {"symbol": symbol, "name": name}

                save_cards_config(config)
                self.send_json({"ok": True, "data": config})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, status=500)

        else:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
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

