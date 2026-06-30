"""
XAUUSD Market Analyzer API — by Gianluca Zumbo
Versione server: nessuna GUI, espone endpoint HTTP per cTrader.
Deploy su Railway.app — completamente gratuito.

Endpoint disponibili:
  GET /signal  → JSON con confidence, bias, summary (usato dal bot cTrader)
  GET /status  → JSON con stato completo e dettagli indicatori
  GET /        → pagina HTML leggibile dal browser
"""

import os
import json
import threading
import traceback
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

# ── Dipendenze ────────────────────────────────────────────────────────────────
try:
    import yfinance as yf
    HAS_YF = True
    # Sessione condivisa con User-Agent realistico: riduce il rate limiting
    # aggressivo che Yahoo applica ai client "anonimi" tipici dei server cloud.
    try:
        import requests as _requests
        _yf_session = _requests.Session()
        _yf_session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0.0.0 Safari/537.36"
        })
    except ImportError:
        _yf_session = None
except ImportError:
    HAS_YF = False
    _yf_session = None
    print("ATTENZIONE: yfinance non disponibile")

try:
    import feedparser
    HAS_FEED = True
except ImportError:
    HAS_FEED = False
    print("ATTENZIONE: feedparser non disponibile")

# ── Configurazione ────────────────────────────────────────────────────────────
UPDATE_INTERVAL_SEC = 300   # aggiorna ogni 5 minuti
PORT                = int(os.environ.get("PORT", 8080))  # Railway imposta PORT automaticamente
API_KEY             = os.environ.get("API_KEY", "GoldZumbo2025")  # chiave di sicurezza

RSS_FEEDS = [
    ("Reuters Business", "https://feeds.reuters.com/reuters/businessNews"),
    ("Reuters Markets",  "https://feeds.reuters.com/reuters/financialNewsHeadlines"),
    ("FT Markets",       "https://www.ft.com/markets?format=rss"),
    ("Investing Gold",   "https://www.investing.com/rss/news_25.rss"),
]

TICKERS = {
    "GOLD":   "GC=F",
    "DXY":    "DX-Y.NYB",
    "US10Y":  "^TNX",
    "SILVER": "SI=F",
    "SPX":    "^GSPC",
    "VIX":    "^VIX",
}

GOLD_BULLISH_WORDS = [
    "gold rally", "gold surges", "gold rises", "safe haven", "haven demand",
    "rate cut", "dovish", "inflation rises", "inflation surge", "cpi higher",
    "geopolitical", "tension", "conflict", "war", "uncertainty", "risk off",
    "dollar falls", "dollar weakens", "dxy down", "fed pause", "fed pivot",
    "recession fear", "bank crisis", "gold demand", "central bank buying",
    "xau rally", "bullion", "gold price up", "gold hits"
]
GOLD_BEARISH_WORDS = [
    "gold falls", "gold drops", "gold slips", "gold tumbles", "gold slides",
    "dollar rallies", "dollar surges", "dxy up", "hawkish", "rate hike",
    "tightening", "strong jobs", "nfp beats", "risk on", "risk appetite",
    "gold selloff", "gold pressure", "gold weakens", "yields rise",
    "bond yields up", "real rates", "xau drops", "gold lower"
]

# ── Stato globale (cache del segnale) ─────────────────────────────────────────
_cache = {
    "result":       None,
    "last_update":  None,
    "updating":     False,
    "error":        None,
}
_cache_lock = threading.Lock()


# ═════════════════════════════════════════════════════════════════════════════
# LOGICA DI ANALISI (identica alla versione locale)
# ═════════════════════════════════════════════════════════════════════════════

class MarketData:
    def __init__(self):
        self.prices   = {}
        self.errors   = []
        self.rss_news = []

    def fetch_all(self):
        self.errors = []
        self._fetch_prices()
        self._fetch_rss()

    def _fetch_prices(self):
        if not HAS_YF:
            self.errors.append("yfinance non disponibile")
            return

        import time

        for name, sym in TICKERS.items():
            success = False
            last_error = None

            # Fino a 3 tentativi con pausa crescente (2s, 5s, 10s)
            for attempt, wait in enumerate([0, 2, 5, 10]):
                if wait > 0:
                    time.sleep(wait)
                try:
                    obj = yf.Ticker(sym, session=_yf_session) if _yf_session else yf.Ticker(sym)
                    df  = obj.history(period="5d", interval="5m")
                    if df is not None and len(df) > 20:
                        self.prices[name] = df
                        success = True
                        break
                    else:
                        last_error = "dati vuoti"
                except Exception as e:
                    last_error = str(e)
                    # Se è un rate limit, vale la pena riprovare con pausa più lunga
                    if "Too Many Requests" in last_error or "rate limit" in last_error.lower():
                        continue
                    else:
                        # Altri errori (es. ticker inesistente) non hanno senso da ritentare
                        break

            if not success:
                self.errors.append(f"{name}: {last_error}")

            # Piccola pausa tra un ticker e l'altro per non bombardare Yahoo
            time.sleep(1.5)

    def _fetch_rss(self):
        if not HAS_FEED:
            self.errors.append("feedparser non disponibile")
            return
        self.rss_news = []
        for label, url in RSS_FEEDS:
            try:
                feed = feedparser.parse(url)
                for entry in feed.entries[:8]:
                    title = getattr(entry, 'title', '')
                    if title:
                        self.rss_news.append({'source': label, 'title': title})
            except Exception as e:
                self.errors.append(f"RSS {label}: {e}")

    def get_ohlc(self, name):
        df = self.prices.get(name)
        return df if df is not None and len(df) >= 2 else None

    def last_close(self, name):
        df = self.get_ohlc(name)
        return float(df['Close'].iloc[-1]) if df is not None else None

    def pct_change(self, name, periods=12):
        df = self.get_ohlc(name)
        if df is None or len(df) < periods + 1: return None
        c = df['Close'].values
        return (c[-1] - c[-periods]) / c[-periods] * 100

    def ema(self, series, period):
        if len(series) < period: return None
        k = 2 / (period + 1)
        e = sum(series[:period]) / period
        for v in series[period:]: e = v * k + e * (1 - k)
        return e

    def rsi(self, series, period=14):
        if len(series) < period + 1: return None
        g = [max(series[-period+i] - series[-period+i-1], 0) for i in range(1, period+1)]
        l = [max(series[-period+i-1] - series[-period+i], 0) for i in range(1, period+1)]
        ag, al = sum(g)/period, sum(l)/period
        return 100 if al == 0 else 100 - 100 / (1 + ag / al)

    def atr(self, df, period=14):
        if df is None or len(df) < period + 1: return None
        trs = []
        for i in range(1, len(df)):
            h  = float(df['High'].iloc[i])
            l  = float(df['Low'].iloc[i])
            pc = float(df['Close'].iloc[i-1])
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        return sum(trs[-period:]) / period


class SignalEngine:
    def __init__(self, data: MarketData):
        self.data = data

    def compute(self):
        scores  = {}
        details = {}

        # Tecnica GOLD
        gold_df = self.data.get_ohlc("GOLD")
        if gold_df is not None and len(gold_df) > 50:
            closes  = list(gold_df['Close'].values.astype(float))
            e9      = self.data.ema(closes, 9)
            e21     = self.data.ema(closes, 21)
            e50     = self.data.ema(closes, 50)
            rsi_val = self.data.rsi(closes, 14)
            atr_val = self.data.atr(gold_df, 14)

            if e9 and e21 and e50:
                if e9 > e21 > e50:
                    scores['ema_gold'] = +25
                    details['EMA Gold'] = "RIALZISTA"
                elif e9 < e21 < e50:
                    scores['ema_gold'] = -25
                    details['EMA Gold'] = "RIBASSISTA"
                else:
                    scores['ema_gold'] = 0
                    details['EMA Gold'] = "NEUTRO"

            if rsi_val:
                if rsi_val > 70:   scores['rsi'] = -10; details['RSI'] = f"IPERCOMPRATO ({rsi_val:.1f})"
                elif rsi_val < 30: scores['rsi'] = +10; details['RSI'] = f"IPERVENDUTO ({rsi_val:.1f})"
                elif rsi_val > 55: scores['rsi'] = +8;  details['RSI'] = f"POSITIVO ({rsi_val:.1f})"
                elif rsi_val < 45: scores['rsi'] = -8;  details['RSI'] = f"NEGATIVO ({rsi_val:.1f})"
                else:              scores['rsi'] = 0;   details['RSI'] = f"NEUTRO ({rsi_val:.1f})"

            if atr_val:
                details['ATR'] = f"{'ALTA' if atr_val > 3.0 else ('BASSA' if atr_val < 1.0 else 'NORMALE')} ({atr_val:.2f})"

            pct1h = self.data.pct_change("GOLD", 12)
            if pct1h is not None:
                if pct1h > 0.3:   scores['momentum'] = +10; details['Momentum 1H'] = f"RIALZISTA (+{pct1h:.2f}%)"
                elif pct1h < -0.3:scores['momentum'] = -10; details['Momentum 1H'] = f"RIBASSISTA ({pct1h:.2f}%)"
                else:              scores['momentum'] = 0;   details['Momentum 1H'] = f"PIATTO ({pct1h:.2f}%)"

        # DXY
        dxy_pct = self.data.pct_change("DXY", 12)
        if dxy_pct is not None:
            if dxy_pct > 0.2:   scores['dxy'] = -15; details['DXY'] = f"IN FORZA (+{dxy_pct:.2f}%)"
            elif dxy_pct < -0.2:scores['dxy'] = +15; details['DXY'] = f"IN CALO ({dxy_pct:.2f}%)"
            else:                scores['dxy'] = 0;   details['DXY'] = f"STABILE ({dxy_pct:.2f}%)"

        # US10Y
        us10y_pct = self.data.pct_change("US10Y", 12)
        if us10y_pct is not None:
            if us10y_pct > 0.5:   scores['yields'] = -12; details['US10Y'] = f"IN RIALZO (+{us10y_pct:.2f}%)"
            elif us10y_pct < -0.5:scores['yields'] = +12; details['US10Y'] = f"IN CALO ({us10y_pct:.2f}%)"
            else:                  scores['yields'] = 0;   details['US10Y'] = f"STABILE ({us10y_pct:.2f}%)"

        # VIX
        vix_val = self.data.last_close("VIX")
        if vix_val is not None:
            if vix_val > 25:   scores['vix'] = +12; details['VIX'] = f"ELEVATO ({vix_val:.1f})"
            elif vix_val < 15: scores['vix'] = -8;  details['VIX'] = f"BASSO ({vix_val:.1f})"
            else:              scores['vix'] = +3;   details['VIX'] = f"MODERATO ({vix_val:.1f})"

        # Silver
        silver_pct = self.data.pct_change("SILVER", 12)
        if silver_pct is not None:
            if silver_pct > 0.3:   scores['silver'] = +8;  details['Silver'] = f"IN RIALZO (+{silver_pct:.2f}%)"
            elif silver_pct < -0.3:scores['silver'] = -8;  details['Silver'] = f"IN CALO ({silver_pct:.2f}%)"
            else:                   scores['silver'] = 0;   details['Silver'] = f"STABILE ({silver_pct:.2f}%)"

        # News RSS
        bull_hits, bear_hits, relevant = 0, 0, []
        for news in self.data.rss_news:
            t = news['title'].lower()
            b = sum(1 for w in GOLD_BULLISH_WORDS if w in t)
            r = sum(1 for w in GOLD_BEARISH_WORDS if w in t)
            if b > 0 or r > 0:
                bull_hits += b; bear_hits += r
                relevant.append(news['title'][:100])

        total_hits = bull_hits + bear_hits
        if total_hits > 0:
            sent_score = int(((bull_hits - bear_hits) / total_hits) * 15)
            scores['news'] = sent_score
            details['News'] = f"{'BULLISH' if sent_score > 3 else ('BEARISH' if sent_score < -3 else 'MISTO')} ({bull_hits}▲ {bear_hits}▼)"
        else:
            scores['news'] = 0
            details['News'] = f"Nessun segnale ({len(self.data.rss_news)} articoli)"

        # Score finale
        raw        = sum(scores.values())
        confidence = max(0, min(100, int(50 + raw * 0.55)))
        bias       = "BULLISH" if raw > 12 else ("BEARISH" if raw < -12 else "NEUTRAL")

        # Summary
        parts = []
        if 'ema_gold' in scores: parts.append(f"{details['EMA Gold']} su EMA")
        if scores.get('dxy', 0) != 0: parts.append(f"DXY {details.get('DXY','')}")
        if scores.get('yields', 0) != 0: parts.append(f"Tassi {details.get('US10Y','')}")
        if total_hits > 0: parts.append(f"News {details.get('News','')}")
        summary = " | ".join(parts) if parts else "Dati insufficienti"

        return {
            "confidence": confidence,
            "bias":       bias,
            "raw_score":  raw,
            "summary":    summary,
            "details":    details,
            "news":       relevant[:5],
            "gold_price": self.data.last_close("GOLD"),
            "dxy_value":  self.data.last_close("DXY"),
            "us10y":      self.data.last_close("US10Y"),
            "vix":        self.data.last_close("VIX"),
            "errors":     self.data.errors,
            "timestamp":  datetime.utcnow().isoformat(timespec='seconds') + "Z",
            "sources_ok": len(self.data.errors) == 0,
        }


# ═════════════════════════════════════════════════════════════════════════════
# AGGIORNAMENTO IN BACKGROUND
# ═════════════════════════════════════════════════════════════════════════════

def _do_update():
    global _cache
    with _cache_lock:
        if _cache["updating"]: return
        _cache["updating"] = True
    try:
        print(f"[{datetime.utcnow().strftime('%H:%M:%S')}] Aggiornamento dati...")
        data   = MarketData()
        data.fetch_all()
        engine = SignalEngine(data)
        result = engine.compute()
        with _cache_lock:
            _cache["result"]      = result
            _cache["last_update"] = datetime.utcnow()
            _cache["error"]       = None
        print(f"[{datetime.utcnow().strftime('%H:%M:%S')}] OK — Confidence: {result['confidence']}/100 | Bias: {result['bias']}")
    except Exception:
        err = traceback.format_exc()
        print(f"ERRORE aggiornamento:\n{err}")
        with _cache_lock:
            _cache["error"] = err
    finally:
        with _cache_lock:
            _cache["updating"] = False


def _scheduler():
    """Aggiorna ogni UPDATE_INTERVAL_SEC in un thread separato."""
    import time
    # Prima esecuzione immediata
    _do_update()
    while True:
        time.sleep(UPDATE_INTERVAL_SEC)
        _do_update()


# ═════════════════════════════════════════════════════════════════════════════
# SERVER HTTP
# ═════════════════════════════════════════════════════════════════════════════

def _get_signal_json():
    """Ritorna il JSON minimale letto dal bot cTrader."""
    with _cache_lock:
        r = _cache["result"]
    if r is None:
        return {"confidence": 0, "bias": "NEUTRAL",
                "summary": "In attesa del primo aggiornamento...",
                "timestamp": datetime.utcnow().isoformat(timespec='seconds') + "Z",
                "sources_ok": False}
    return {
        "confidence": r["confidence"],
        "bias":       r["bias"],
        "summary":    r["summary"],
        "timestamp":  r["timestamp"],
        "sources_ok": r["sources_ok"],
    }


def _get_status_json():
    """Ritorna il JSON completo con tutti i dettagli."""
    with _cache_lock:
        r   = _cache["result"]
        lu  = _cache["last_update"]
        err = _cache["error"]
    if r is None:
        return {"status": "initializing", "error": err}
    age = int((datetime.utcnow() - lu).total_seconds()) if lu else -1
    return {**r, "age_seconds": age, "server_error": err}


def _get_html():
    with _cache_lock:
        r   = _cache["result"]
        lu  = _cache["last_update"]
    if r is None:
        body = "<p>⏳ In attesa del primo aggiornamento... (max 5 minuti)</p>"
    else:
        conf  = r['confidence']
        bias  = r['bias']
        color = "#1DB954" if conf >= 65 else ("#E63946" if conf <= 40 else "#C9A84C")
        bias_icon = "▲" if bias == "BULLISH" else ("▼" if bias == "BEARISH" else "◆")
        age = int((datetime.utcnow() - lu).total_seconds()) if lu else 0
        det_rows = "".join(
            f"<tr><td>{k}</td><td>{v}</td></tr>"
            for k, v in r.get('details', {}).items()
        )
        news_items = "".join(f"<li>{n}</li>" for n in r.get('news', []))
        err_html = ""
        if r.get('errors'):
            err_html = "<p style='color:#E63946'>⚠ " + " | ".join(r['errors']) + "</p>"
        gold = r.get('gold_price'); gold_str = f"${gold:,.2f}" if gold else "n/d"
        body = f"""
        <div class="card">
          <div class="big" style="color:{color}">{conf}<span class="sub">/100</span></div>
          <div class="bias">{bias_icon} {bias}</div>
          <div class="summary">{r['summary']}</div>
        </div>
        <div class="card">
          <b>Gold:</b> {gold_str} &nbsp;|&nbsp;
          <b>Età:</b> {age}s &nbsp;|&nbsp;
          <b>Aggiornato:</b> {lu.strftime('%H:%M:%S UTC') if lu else 'n/d'}
          {err_html}
        </div>
        <div class="card">
          <table><tr><th>Indicatore</th><th>Valore</th></tr>{det_rows}</table>
        </div>
        {'<div class="card"><b>News rilevanti:</b><ul>' + news_items + '</ul></div>' if news_items else ''}
        """
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="60">
<title>XAUUSD Analyzer — Gianluca Zumbo</title>
<style>
  body{{background:#0D0D0D;color:#E8E8E8;font-family:monospace;padding:20px;margin:0}}
  h1{{color:#C9A84C;border-bottom:1px solid #2A2A2A;padding-bottom:8px}}
  .card{{background:#151515;border:1px solid #2A2A2A;border-radius:6px;padding:16px;margin:12px 0}}
  .big{{font-size:64px;font-weight:bold;line-height:1}}
  .sub{{font-size:24px;color:#888}}
  .bias{{font-size:22px;font-weight:bold;margin:8px 0}}
  .summary{{color:#888;font-size:13px;margin-top:8px}}
  table{{width:100%;border-collapse:collapse}}
  th,td{{text-align:left;padding:6px 10px;border-bottom:1px solid #2A2A2A;font-size:13px}}
  th{{color:#C9A84C}}
  ul{{margin:4px 0;padding-left:18px;font-size:12px;color:#888}}
</style></head>
<body>
<h1>◈ XAUUSD Market Analyzer — Gianluca Zumbo</h1>
{body}
<p style="color:#444;font-size:11px">Aggiornamento automatico ogni 5 minuti. Pagina si ricarica ogni 60s.</p>
</body></html>"""


class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        # Sopprime i log HTTP per non spammare il terminale
        pass

    def _send(self, code, content_type, body):
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _check_api_key(self):
        """Verifica la chiave API nell'header X-API-Key o nel query string."""
        key_header = self.headers.get("X-API-Key", "")
        key_query  = ""
        if "?" in self.path:
            for part in self.path.split("?", 1)[1].split("&"):
                if part.startswith("key="):
                    key_query = part[4:]
        return key_header == API_KEY or key_query == API_KEY

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/":
            self._send(200, "text/html; charset=utf-8", _get_html())

        elif path == "/signal":
            if not self._check_api_key():
                self._send(401, "application/json",
                           json.dumps({"error": "API key mancante o errata"}))
                return
            self._send(200, "application/json",
                       json.dumps(_get_signal_json(), ensure_ascii=False))

        elif path == "/status":
            if not self._check_api_key():
                self._send(401, "application/json",
                           json.dumps({"error": "API key mancante o errata"}))
                return
            self._send(200, "application/json",
                       json.dumps(_get_status_json(), default=str, ensure_ascii=False))

        elif path == "/health":
            # Endpoint pubblico per Railway health check
            self._send(200, "application/json", '{"status":"ok"}')

        else:
            self._send(404, "application/json", '{"error":"not found"}')

    def do_HEAD(self):
        self._send(200, "text/html", "")


# ═════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("="*55)
    print("  XAUUSD Market Analyzer API — Gianluca Zumbo")
    print(f"  Porta: {PORT} | Aggiornamento: ogni {UPDATE_INTERVAL_SEC}s")
    print(f"  API Key: {API_KEY}")
    print("="*55)

    # Avvia il thread di aggiornamento dati
    t = threading.Thread(target=_scheduler, daemon=True)
    t.start()

    # Avvia il server HTTP
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"  Server avviato su http://0.0.0.0:{PORT}")
    print(f"  Endpoint: /signal?key={API_KEY}")
    print("="*55)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer fermato.")
