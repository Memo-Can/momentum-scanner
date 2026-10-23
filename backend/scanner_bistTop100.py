#!/usr/bin/env python3
"""BIST is katmani (sunum icermez). Web/terminal arayuzlerinin BIST 30 / BIST
100 / BIST 300 sekmelerini besleyen TEK BIRLESIK havuzu hesaplar:
  - BIST 30 / BIST 100: resmi endeks uyeligi (sabit liste - BIST30_TICKERS/
    BIST100_TICKERS). BIST30, BIST100'un bir alt kumesi oldugu icin
    BIST100'u taramak otomatik olarak BIST30'u da kapsar.
  - BIST 300: gunluk % degisime gore (yuksekten dusuge) dinamik ilk 300
    hisse (fetch_top_gainers) - resmi bir endeks degil.
Havuz = BIST100_TICKERS UNION fetch_top_gainers(TOP300_SIZE) (cakisanlar
TEKRAR CEKILMEZ) - boylece TUM piyasayi (~600+ hisse) taramak yerine en
fazla ~100+300=400 farklı hisse cekilir, Yahoo'nun resmi olmayan API'sinin
rate-limit riskini azaltir. Her hisse icin EMA/MACD/RSI/Bollinger/ADX/ATR
indikatorleri hesaplanip tek bir Skor'a (-6..+6, GUCLU_AL..GUCLU_SAT)
birlestirilir. Bu modul dogrudan calistirilmaz - terminal_bistTop100.py
(terminal arayuzu) ve web_bistTop100.py (web arayuzu) bu modulu
`import scanner_bistTop100 as scanner` ile kullanip kendi sunumlarini yapar.

Her REFRESH_SECONDS (300sn / 5dk) saniyede bir veri Yahoo Finance'ten
yeniden cekilir.

Onbellek: 5dk sinyali ve haftalik/aylik her REFRESH_SECONDS'ta Yahoo'dan
yeniden cekilir (onbellek yok). Sadece BIST100 endeksi (RS icin) dongu
basina onbelleklenir - yoksa her hisse icin ayri ayri cekilip 100+ kat
gereksiz istege yol acardi.

Sinyal gecmisi: Skoru GUCLU_AL/GUCLU_SAT'a DONEN hisseler signal_log_bist.csv
dosyasina eklenir (sadece durum degistiginde, spam onlenir) ve kayitli mobil
cihazlara push bildirimi gonderilir. Zamanla bu kayitlara bakip Skor'un
gercekte ise yarayip yaramadigini (sinyalden sonra fiyat ne oldu) kendi
verinizle degerlendirebilirsiniz.
"""

import json
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

# firebase-admin henuz kurulu olmayabilir (requirements.txt'e yeni eklendi) -
# import basarisiz olursa push bildirimleri sessizce devre disi kalir, geri
# kalan tum tarama/skor mantigi normal calismaya devam eder.
try:
    import firebase_admin
    from firebase_admin import credentials, messaging
except ImportError:
    firebase_admin = None

REFRESH_SECONDS = 300  # 5 dakika
MAX_RETRIES = 2
TOP300_SIZE = 300  # BIST 300 sekmesi: gunluk % degisime gore ilk N (dinamik, endeks degil)

# Resmi BIST 30 Endeksi'nin (XU030) sabit uye listesi - 2026-09 itibariyla
# dogrulanmis (Midas + Yahoo region=tr evreniyle capraz kontrol edildi).
# BIST30, BIST100_TICKERS'in bir ALT KUMESIDIR (dogrulandi) - bu yuzden
# BIST100 taramasi otomatik olarak BIST30'u da kapsar, ayri bir istek
# gerekmez.
BIST30_TICKERS = [
    "AEFES", "AKBNK", "ASELS", "ASTOR", "BIMAS", "DSTKF", "EKGYO", "ENKAI",
    "EREGL", "FROTO", "GARAN", "GUBRF", "ISCTR", "KCHOL", "KRDMD", "MGROS",
    "PETKM", "PGSUS", "SAHOL", "SASA", "SISE", "TAVHL", "TCELL", "THYAO",
    "TOASO", "TRALT", "TTKOM", "TUPRS", "VAKBN", "YKBNK",
]
BIST30_TICKERS = [f"{t}.IS" for t in BIST30_TICKERS]

# Resmi BIST 100 Endeksi'nin (XU100) sabit uye listesi - 2026-09 itibariyla
# dogrulanmis (Midas + Yahoo region=tr evreniyle capraz kontrol edildi);
# endeks ceyreklik yeniden dengelendigi icin zamanla guncellenmesi gerekebilir.
BIST100_TICKERS = [
    "AEFES", "AKBNK", "AKSA", "AKSEN", "ALARK", "ALTNY", "ANSGR", "ARCLK",
    "ASELS", "ASTOR", "BALSU", "BERA", "BIMAS", "BRSAN", "BRYAT", "BSOKE",
    "BTCIM", "CANTE", "CCOLA", "CIMSA", "CVKMD", "CWENE", "DAPGM", "DOAS",
    "DOHOL", "DSTKF", "ECILC", "EFOR", "EKGYO", "ENERY", "ENJSA", "ENKAI",
    "EREGL", "ESEN", "EUPWR", "EUREN", "FENER", "FROTO", "GARAN", "GENIL",
    "GESAN", "GLRMK", "GRSEL", "GRTHO", "GSRAY", "GUBRF", "HALKB", "HEKTS",
    "IEYHO", "ISCTR", "ISMEN", "IZENR", "KCHOL", "KLRHO", "KRDMD", "KTLEV",
    "KUYAS", "MAGEN", "MAVI", "MGROS", "MIATK", "MPARK", "OBAMS", "ODAS",
    "ODINE", "OTKAR", "OYAKC", "PAHOL", "PASEU", "PATEK", "PETKM", "PGSUS",
    "PSGYO", "QUAGR", "RALYH", "REEDR", "SAHOL", "SARKY", "SASA", "SISE",
    "SKBNK", "SOKM", "TAVHL", "TCELL", "THYAO", "TKFEN", "TOASO", "TRALT",
    "TRENJ", "TRMET", "TSKB", "TTKOM", "TUKAS", "TUPRS", "TURSG", "ULKER",
    "VAKBN", "VESTL", "YKBNK", "ZOREN",
]
BIST100_TICKERS = [f"{t}.IS" for t in BIST100_TICKERS]

# GUCLU_AL/GUCLU_SAT sinyallerinin gecmisini ve her dongudeki skor gecmisini
# kaydeder - zamanla bu kayitlara bakip Skor'un gercekte ise yarayip
# yaramadigini (sinyalden sonra fiyat ne oldu) kendi verinizle
# degerlendirebilirsiniz. Eskiden iki ayri CSV cifti (canli + backfill) idi;
# 2026-10'da tek bir SQLite dosyasina (iki tabloya, source='live'/'backfill'
# sutunuyla ayristirilarak) tasindi - bkz. migrate_csv_to_sqlite.py. Sebep:
# canli score_history CSV'si milyonlarca satira cikinca (3M+/130MB+) her
# uygulama baslangicinda tamami belleğe okunuyordu (bkz. CLAUDE.md), SQLite
# indeksli sorgu sayesinde bu gerekmiyor.
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "momentum_scanner.db")
DEVICE_TOKENS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "device_tokens_bist.json")

# Sanal para (paper trading) simulasyonu: GUCLU_AL'a YENI GECEN hisseler icin
# otomatik "satin alma", swing stop/hedef (asagida ATR_SWING_MULT/ATR_TARGET_R)
# ya da GUCLU_SAT'a donus ya da sure asimi ile "satma". Amac: Konfluens
# Skor'un gercekte bir edge saglayip saglamadigini kendi canli verimizle
# ampirik olcmek. _init_paper_trading() bu sabitlere ihtiyac duydugu icin
# (asagida cagriliyor) burada, DB kurulumundan ONCE tanimlanmalari gerekiyor.
PAPER_STARTING_BALANCE = 1_000_000.0
PAPER_MAX_POSITIONS = 5
PAPER_MAX_HOLD_DAYS = 21  # ~15 islem gunune denk takvim gunu yaklasimi

# SQLite tek-yazarli oldugu icin tum INSERT/DELETE'ler bu kilit altinda
# yapilir - WAL modu zaten coklu OKUYUCUYA izin veriyor (Flask'in threaded=True
# istek thread'leri bu yuzden yazma sirasinda bile okuyabilir), ama ayni
# process icinde iki yazma ayni anda calismasin diye bu ekstra guvenlik var.
_DB_LOCK = threading.Lock()


def get_db_connection() -> sqlite3.Connection:
    """Her cagrida KISA OMURLU yeni bir baglanti acar (SQLite'ta bu ucuz bir
    islem) - thread'ler arasinda tek bir Connection nesnesini paylasmanin
    getirdigi "ayni anda kullanim" guvenlik sorunlarindan kacinmak icin.
    WAL modu: process/thread'ler arasi coklu okuyucu + bir yazarin birbirini
    BLOKLAMADAN calismasini saglar."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    """Modul import edilirken BIR KEZ cagrilir - tablo/indeksler yoksa
    olusturur (CREATE TABLE IF NOT EXISTS, idempotent)."""
    with _DB_LOCK:
        conn = get_db_connection()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS score_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    score_raw REAL,
                    score TEXT,
                    price REAL,
                    source TEXT NOT NULL DEFAULT 'live'
                );
                CREATE INDEX IF NOT EXISTS idx_score_history_ticker_ts
                    ON score_history(ticker, timestamp);

                CREATE TABLE IF NOT EXISTS signal_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    score TEXT,
                    price REAL,
                    daily_pct REAL,
                    rsi REAL,
                    adx REAL,
                    rel_strength REAL,
                    source TEXT NOT NULL DEFAULT 'live'
                );
                CREATE INDEX IF NOT EXISTS idx_signal_log_ticker_ts
                    ON signal_log(ticker, timestamp);

                CREATE TABLE IF NOT EXISTS paper_account (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    cash REAL NOT NULL,
                    starting_balance REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS paper_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    entry_time TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    shares REAL NOT NULL,
                    allocation REAL NOT NULL,
                    stop_price REAL,
                    target_price REAL,
                    max_hold_until TEXT NOT NULL,
                    exit_time TEXT,
                    exit_price REAL,
                    exit_reason TEXT,
                    pnl REAL
                );
                CREATE INDEX IF NOT EXISTS idx_paper_trades_status
                    ON paper_trades(status);
                CREATE INDEX IF NOT EXISTS idx_paper_trades_ticker
                    ON paper_trades(ticker);

                CREATE TABLE IF NOT EXISTS paper_equity_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    cash REAL NOT NULL,
                    positions_value REAL NOT NULL,
                    equity REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_paper_equity_ts
                    ON paper_equity_history(timestamp);
                """
            )
            conn.commit()
        finally:
            conn.close()


def _init_paper_trading() -> None:
    """_init_db()'nin yanında modul import'unda BIR KEZ cagrilir -
    paper_account tablosunda satir yoksa PAPER_STARTING_BALANCE ile
    bootstrap eder (idempotent - satir zaten varsa dokunmaz)."""
    with _DB_LOCK:
        conn = get_db_connection()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO paper_account (id, cash, starting_balance) VALUES (1, ?, ?)",
                (PAPER_STARTING_BALANCE, PAPER_STARTING_BALANCE),
            )
            conn.commit()
        finally:
            conn.close()


_init_db()
_init_paper_trading()

# Ticker -> en son gorulen skor. log_signal_changes'in _last_logged_score'undan
# BAGIMSIZ, ayri bir takip (push bildirim/sinyal log mantigina dokunulmaz).
# Process her (re)basladiginda BOS baslar - bu KASITLI: ilk dongude her ticker
# icin prev=None olacagi icin "yeni gecis" sayilmaz, yani deploy/restart
# aninda zaten GUCLU_AL olan hicbir sey satin alinmaz. Ikinci dongudan
# itibaren gercek gecisler (prev != 'GUCLU_AL' iken score=='GUCLU_AL' olmasi)
# yakalanir.
_paper_last_score = {}

# Firebase kimlik dosyasi (servis hesabi JSON'u) commit'lenmez - VPS'te
# systemd servis dosyasinda FIREBASE_CREDENTIALS_PATH env degiskeniyle
# gosterilir. Dosya yoksa (henuz Firebase projesi kurulmadiysa) push
# bildirimleri sessizce devre disi kalir, scanner normal calismaya devam eder.
if firebase_admin is not None:
    _firebase_cred_path = os.environ.get("FIREBASE_CREDENTIALS_PATH")
    if _firebase_cred_path and os.path.isfile(_firebase_cred_path) and not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(_firebase_cred_path))

EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
BB_PERIOD = 20
BB_STD = 2
ADX_PERIOD = 14
ADX_TREND_THRESHOLD = 20  # bu esigin altinda piyasa zayif/yatay (gurultu) sayilir
BENCHMARK_TICKER = "XU100.IS"  # BIST100 endeksi - goreceli guc (RS) karsilastirma bazı
RS_THRESHOLD = 0.5  # bu esigin altindaki fark (yuzde puan) NOTR sayilir
ATR_PERIOD = 14
ATR_DAY_MULT = 1.5  # gunluk (day trade) stop mesafesi = ATR * bu katsayi (dar)
ATR_SWING_MULT = 3.0  # swing stop mesafesi = ATR * bu katsayi (genis)
ATR_TARGET_R = 2.0  # hedef mesafesi = stop mesafesi * bu katsayi (risk:odul ~1:2)
WEEKLY_LOOKBACK_DAYS = 5  # "5D" - kayan pencere, takvim haftasi degil
MONTHLY_LOOKBACK_DAYS = 21  # "1M" - ~1 aylik islem gunu sayisi, kayan pencere

# Yahoo'nun standart (Ingilizce) sektor isimlerini kisa Turkce kodlara cevirir.
# Bu dosyada hisse listesi dinamik oldugu icin (sabit BIST30/100 degil), sabit
# bir ticker->sektor haritasi yerine Yahoo'nun her hisse icin dondurdugu genel
# "sector" alani kullanilir - boylece tum BIST evreni kapsanmis olur.
SECTOR_TRANSLATE = {
    "Technology": "TEKN",
    "Financial Services": "FINN",
    "Healthcare": "SAGL",
    "Consumer Cyclical": "TUKC",
    "Consumer Defensive": "TUKD",
    "Industrials": "SANY",
    "Basic Materials": "MALZ",
    "Energy": "ENRJ",
    "Utilities": "KAMU",
    "Real Estate": "GYO",
    "Communication Services": "ILET",
}


def pct_change(current: float, past: float) -> float:
    if past in (0, None) or current is None:
        return float("nan")
    return (current - past) / past * 100.0


def rolling_pct_change(closes, current_price: float, periods: int) -> float:
    """N ISLEM GUNU onceki kapanis ile canli fiyat arasindaki yuzde degisimi
    dondurur - kayan/rolling pencere (Yahoo'nun web sitesindeki '5D'/'1M'
    gosterimiyle ayni mantik: takvim haftasi/ayi basindan degil, sabit sayida
    islem gunu geriye gider). Zaten cekilmis gunluk kapanis serisi (closes)
    uzerinden hesaplanir, ekstra Yahoo istegi gerekmez."""
    if closes is None or len(closes) <= periods:
        return float("nan")
    past = float(closes.iloc[-(periods + 1)])
    return pct_change(current_price, past)




_benchmark_cache = {}  # "pct" -> (fetched_at, daily_pct)


def get_benchmark_daily_pct():
    """BIST100 endeksinin gunluk yuzde degisimini REFRESH_SECONDS'a yakin bir
    sure onbellekten dondurur - her hisse icin ayri ayri degil, dongu basina
    bir kez cekilir (onbellek olmadan, 100+ hisse icin ayni endeks verisi
    100+ kez ayri ayri cekilirdi). Hisselerin gercekten piyasayla birlikte mi
    surukendigi yoksa piyasadan bagimsiz mi guclu oldugu (RS) bunun uzerinden
    hesaplanir."""
    cached = _benchmark_cache.get("pct")
    if cached and time.time() - cached[0] < REFRESH_SECONDS - 5:
        return cached[1]
    pct = None
    try:
        tk = yf.Ticker(BENCHMARK_TICKER)
        info = tk.get_info()
        pct = info.get("regularMarketChangePercent")
        if pct is None:
            hist = tk.history(period="5d", interval="1d", auto_adjust=False)
            if len(hist) >= 2:
                pct = pct_change(float(hist["Close"].iloc[-1]), float(hist["Close"].iloc[-2]))
    except Exception:
        pct = None
    _benchmark_cache["pct"] = (time.time(), pct)
    return pct


GRAMS_PER_TROY_OUNCE = 31.1034768


def _quote_price_and_pct(ticker: str):
    """Verilen Yahoo sembolu icin (guncel fiyat, gunluk % degisim) dondurur -
    fetch_row()'daki ayni fiyat/degisim okuma mantigi, tek bir hisse yerine
    herhangi bir enstruman (doviz/emtia/endeks/kripto) icin genellenmis hali."""
    try:
        info = yf.Ticker(ticker).get_info()
        price = info.get("regularMarketPrice")
        if price is None:
            return None, None
        pct = info.get("regularMarketChangePercent")
        if pct is None:
            prev = info.get("regularMarketPreviousClose")
            pct = pct_change(price, prev) if prev else None
        return price, pct
    except Exception:
        return None, None


def fetch_market_indicators():
    """Ust seritte gosterilen 8 piyasa gostergesini (doviz/emtia/BIST100/BTC)
    dondurur - her biri {label, price, pct, unit} seklinde. Gram altin/gumus
    Yahoo'da dogrudan yok; ons fiyati (USD) ile USDTRY kurunun TURETILMIS
    (derived) carpimidir - gunluk % degisimi de onceki gunun (fiyat/(1+%/100))
    ile geriye hesaplanan turetilmis onceki degerden hesaplanir."""
    usd_price, usd_pct = _quote_price_and_pct("USDTRY=X")
    eur_price, eur_pct = _quote_price_and_pct("EURTRY=X")
    gbp_price, gbp_pct = _quote_price_and_pct("GBPTRY=X")
    xu100_price, xu100_pct = _quote_price_and_pct("XU100.IS")
    btc_price, btc_pct = _quote_price_and_pct("BTC-USD")
    gold_oz_price, gold_oz_pct = _quote_price_and_pct("GC=F")
    silver_oz_price, silver_oz_pct = _quote_price_and_pct("SI=F")
    oil_price, oil_pct = _quote_price_and_pct("BZ=F")

    def _derived_gram(oz_price, oz_pct):
        if oz_price is None or usd_price is None:
            return None, None
        gram_try = oz_price * usd_price / GRAMS_PER_TROY_OUNCE
        if oz_pct is None or usd_pct is None:
            return gram_try, None
        prev_oz = oz_price / (1 + oz_pct / 100)
        prev_usd = usd_price / (1 + usd_pct / 100)
        prev_gram = prev_oz * prev_usd / GRAMS_PER_TROY_OUNCE
        return gram_try, pct_change(gram_try, prev_gram)

    gold_gram_price, gold_gram_pct = _derived_gram(gold_oz_price, gold_oz_pct)
    silver_gram_price, silver_gram_pct = _derived_gram(silver_oz_price, silver_oz_pct)

    return [
        {"label": "Dolar", "price": usd_price, "pct": usd_pct, "unit": "TL"},
        {"label": "Euro", "price": eur_price, "pct": eur_pct, "unit": "TL"},
        {"label": "Sterlin", "price": gbp_price, "pct": gbp_pct, "unit": "TL"},
        {"label": "BIST100", "price": xu100_price, "pct": xu100_pct, "unit": ""},
        {"label": "Bitcoin", "price": btc_price, "pct": btc_pct, "unit": "$"},
        {"label": "Gram Altin", "price": gold_gram_price, "pct": gold_gram_pct, "unit": "TL"},
        {"label": "Gram Gumus", "price": silver_gram_price, "pct": silver_gram_pct, "unit": "TL"},
        {"label": "Petrol", "price": oil_price, "pct": oil_pct, "unit": "$"},
    ]


def get_intraday_signal(tk) -> str:
    """5dk mum uzerindeki surekli EMA9/EMA21 trend durumunu (kesisim ani degil)
    dondurur."""
    intraday = tk.history(period="5d", interval="5m", auto_adjust=False)
    return ema_trend_signal(intraday["Close"] if not intraday.empty else None)


def ema_trend_signal(closes) -> str:
    """EMA9'un EMA21'e gore o anki surekli trend durumunu dondurur (kesisim
    ani degil). EMA9 > EMA21 oldugu surece AL, EMA9 < EMA21 oldugu surece SAT
    gosterilmeye devam eder; trend degismeden AL/SAT durumu sabit kalir."""
    if closes is None or len(closes) < EMA_SLOW + 1:
        return "NOTR"
    ema_fast = closes.ewm(span=EMA_FAST, adjust=False).mean()
    ema_slow = closes.ewm(span=EMA_SLOW, adjust=False).mean()
    diff = ema_fast.iloc[-1] - ema_slow.iloc[-1]
    if diff > 0:
        return "AL"
    if diff < 0:
        return "SAT"
    return "NOTR"


def rsi(closes, period: int = RSI_PERIOD) -> float:
    """Wilder RSI(14) degerini dondurur (0-100). >=70 asiri alim, <=30 asiri
    satim olarak yorumlanir; momentum taramasinda RSI'nin 50 uzerinde
    kalmasi trendin devam ettigine dair ek teyit sayilir."""
    if closes is None or len(closes) < period + 1:
        return float("nan")
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean().iloc[-1]
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean().iloc[-1]
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def macd_signal(closes, fast: int = MACD_FAST, slow: int = MACD_SLOW, signal: int = MACD_SIGNAL) -> str:
    """MACD histogramin (MACD cizgisi - sinyal cizgisi) isaretine gore
    surekli AL/SAT/NOTR dondurur (kesisim ani degil, EMA9/21 sinyaliyle
    ayni mantik)."""
    if closes is None or len(closes) < slow + signal:
        return "NOTR"
    ema_fast = closes.ewm(span=fast, adjust=False).mean()
    ema_slow = closes.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line.iloc[-1] - signal_line.iloc[-1]
    if histogram > 0:
        return "AL"
    if histogram < 0:
        return "SAT"
    return "NOTR"


def bollinger_percent_b(closes, period: int = BB_PERIOD, num_std: float = BB_STD) -> float:
    """Fiyatin Bollinger bantlari icindeki yerini (%B) dondurur: 0%=alt bant,
    100%=ust bant. Bandin disina cikan deger (>100%/<0%), hacimle destekliyse
    asiri alim degil guclu kirilim/momentum olarak okunur."""
    if closes is None or len(closes) < period:
        return float("nan")
    sma = closes.rolling(period).mean().iloc[-1]
    std = closes.rolling(period).std().iloc[-1]
    if std != std or std == 0:
        return float("nan")
    upper = sma + num_std * std
    lower = sma - num_std * std
    return (closes.iloc[-1] - lower) / (upper - lower) * 100.0


def ema_series_pair(closes, fast: int = EMA_FAST, slow: int = EMA_SLOW):
    """ema_trend_signal'in TAM zaman serisi hali - grafikte EMA9/21 cizgilerini
    cizebilmek icin. Ayni ewm formulu, sadece son deger yerine tum seri doner."""
    ema_fast = closes.ewm(span=fast, adjust=False).mean()
    ema_slow = closes.ewm(span=slow, adjust=False).mean()
    return ema_fast, ema_slow


def rsi_series(closes, period: int = RSI_PERIOD):
    """rsi()'nin TAM zaman serisi hali - grafikte RSI cizgisini cizebilmek icin."""
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def macd_series(closes, fast: int = MACD_FAST, slow: int = MACD_SLOW, signal: int = MACD_SIGNAL):
    """macd_signal()'in TAM zaman serisi hali - grafikte MACD/sinyal/histogram
    cizgilerini cizebilmek icin."""
    ema_fast = closes.ewm(span=fast, adjust=False).mean()
    ema_slow = closes.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def bollinger_bands_series(closes, period: int = BB_PERIOD, num_std: float = BB_STD):
    """bollinger_percent_b()'nin TAM zaman serisi hali - grafikte ust/orta/alt
    bant cizgilerini cizebilmek icin."""
    sma = closes.rolling(period).mean()
    std = closes.rolling(period).std()
    upper = sma + num_std * std
    lower = sma - num_std * std
    return upper, sma, lower


def adx_series(hist, period: int = ADX_PERIOD):
    """adx()'in TAM zaman serisi hali - gecmise donuk (backfill) her gun
    icin ADX degerini hesaplamak amaciyla. Ayni Wilder formulu, sadece son
    deger yerine tum seri doner."""
    high = hist["High"]
    low = hist["Low"]
    close = hist["Close"]
    prev_close = close.shift(1)

    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)

    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = ((up_move > down_move) & (up_move > 0)) * up_move
    minus_dm = ((down_move > up_move) & (down_move > 0)) * down_move

    smoothed_tr = tr.ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / smoothed_tr
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / smoothed_tr

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    return dx.ewm(alpha=1 / period, adjust=False).mean()


def adx(hist, period: int = ADX_PERIOD) -> float:
    """Wilder ADX(14) degerini dondurur (0-100). ADX yonu degil trendin
    GUCUNU olcer: <20 zayif/yatay piyasa (sinyaller gurultulu olabilir),
    >=25 gercek/guclu trend sayilir. Skor'un GUCLU AL/SAT etiketini
    onaylamak icin guven filtresi olarak kullanilir."""
    if hist is None or len(hist) < period * 2:
        return float("nan")
    adx_val = adx_series(hist, period).iloc[-1]
    return float(adx_val) if adx_val == adx_val else float("nan")


def atr(hist, period: int = ATR_PERIOD) -> float:
    """Wilder ATR(14) - ortalama gercek aralik (Average True Range), fiyatin
    ortalama gunluk oynakligini olcer. Stop-loss/hedef mesafesini oynakliga
    gore olceklemek icin kullanilir (sabit %X yerine, hissenin kendi
    karakterine uyarlanmis bir mesafe)."""
    if hist is None or len(hist) < period + 1:
        return float("nan")
    high = hist["High"]
    low = hist["Low"]
    close = hist["Close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    val = tr.ewm(alpha=1 / period, adjust=False).mean().iloc[-1]
    return float(val) if val == val else float("nan")


def stop_target_levels(price: float, atr_val: float, direction: str, stop_mult: float, target_r: float):
    """Verilen ATR mesafesine gore stop-loss ve hedef fiyat seviyelerini
    dondurur. direction 'AL' ise stop asagida/hedef yukarida, 'SAT' ise
    tam tersi (kisa pozisyon varsayimi). NOTR ya da veri yoksa (None, None)
    dondurur - net bir yon olmadan stop/hedef onermenin anlami yok."""
    if price != price or atr_val != atr_val or direction not in ("AL", "SAT"):
        return None, None
    distance = atr_val * stop_mult
    if direction == "AL":
        stop = price - distance
        target = price + distance * target_r
    else:
        stop = price + distance
        target = price - distance * target_r
    return stop, target


def confluence_score_raw(
    signal_1d: str,
    signal_5m: str,
    signal_macd: str,
    rsi_val: float,
    bb_percent: float,
    rel_strength: float,
) -> int:
    """confluence_label'in ADX filtresi uygulanmadan onceki ham -6..+6
    toplami. ADX aninlik bir guven filtresi oldugu icin (ortalamasi anlamli
    degil), Skor Gecmisi sekmelerinde gunluk/saatlik ortalama almak icin bu
    ham deger ayrica saklanir - bkz. log_score_history."""

    def cat(sig: str) -> int:
        return {"AL": 1, "SAT": -1}.get(sig, 0)

    score = cat(signal_1d) + cat(signal_5m) + cat(signal_macd)

    if rsi_val == rsi_val:  # NaN degil
        if rsi_val >= 55:
            score += 1
        elif rsi_val <= 45:
            score -= 1

    if bb_percent == bb_percent:  # NaN degil
        score += 1 if bb_percent >= 50 else -1

    if rel_strength == rel_strength:  # NaN degil
        if rel_strength >= RS_THRESHOLD:
            score += 1
        elif rel_strength <= -RS_THRESHOLD:
            score -= 1

    return score


def confluence_label(
    signal_1d: str,
    signal_5m: str,
    signal_macd: str,
    rsi_val: float,
    bb_percent: float,
    adx_val: float,
    rel_strength: float,
) -> str:
    """1d/5m EMA, MACD, RSI, BB% ve endekse gore goreceli guc (RS) sinyallerini
    tek bir konfluens skoruna (-6..+6) birlestirir ve GUCLU_AL/AL/NOTR/SAT/
    GUCLU_SAT etiketine cevirir. Boylece 6 ayri sutuna tek tek bakip kafada
    birlestirmek yerine, tek sutunda net bir AL/SAT karari gorulur.

    RS (rel_strength = hissenin gunluk% - endeksin gunluk%): hisse piyasayla
    birlikte mi surukleniyor yoksa piyasadan bagimsiz gercekten guclu mu
    oldugunu ayirt eder - endeksin +%4 oldugu bir gunde hissenin +%5 olmasi,
    endeksin duz oldugu bir gunde +%5 olmasindan cok daha az anlamlidir.

    ADX, ayri bir sutun olarak degil, GUCLU_AL/GUCLU_SAT etiketinin
    guvenilirligini teyit eden bir filtre olarak kullanilir: ADX<20 (zayif/
    yatay piyasa) veya hesaplanamiyorsa, "guclu" etiket normal AL/SAT'a
    dusurulur - boyle bir ortamda guclu sinyale guvenmek yaniltici olabilir."""

    score = confluence_score_raw(signal_1d, signal_5m, signal_macd, rsi_val, bb_percent, rel_strength)

    if score >= 5:
        label = "GUCLU_AL"
    elif score >= 2:
        label = "AL"
    elif score >= -1:
        label = "NOTR"
    elif score >= -4:
        label = "SAT"
    else:
        label = "GUCLU_SAT"

    trend_confirmed = adx_val == adx_val and adx_val >= ADX_TREND_THRESHOLD
    if label == "GUCLU_AL" and not trend_confirmed:
        label = "AL"
    elif label == "GUCLU_SAT" and not trend_confirmed:
        label = "SAT"

    return label


def build_bist_query():
    """Yahoo'nun BIST (Borsa Istanbul) icin TUM piyasayi tarayan sorgusu -
    region="tr" butun BIST hisselerini kapsar, endeks uyeligi ayrimi/esik
    yoktur."""
    return yf.EquityQuery("eq", ["region", "tr"])


SCREEN_PAGE_SIZE = 250  # Yahoo'nun yf.screen() tek istekte donduregi maksimum sonuc sayisi


def fetch_all_bist_tickers():
    """Yahoo'nun region=tr taramasindaki TUM hisseleri sayfalama ile (tek
    istek max SCREEN_PAGE_SIZE donduruyor) toplar - hem canli tarama
    (terminal_bistTop100.py/web_bistTop100.py) hem de backfill_daily_bist.py
    tarafindan kullanilir."""
    query = build_bist_query()
    symbols = []
    offset = 0
    try:
        while True:
            result = yf.screen(query, sortField="percentchange", sortAsc=False, size=SCREEN_PAGE_SIZE, offset=offset)
            quotes = result.get("quotes", [])
            if not quotes:
                break
            symbols.extend(q["symbol"] for q in quotes if q.get("symbol"))
            offset += len(quotes)
            if offset >= result.get("total", 0):
                break
    except Exception:
        pass
    return symbols


def fetch_top_gainers(n: int):
    """Yahoo'nun region=tr taramasindan gunluk % degisime gore (yuksekten
    dusuge) ilk n hisseyi sayfalayarak dondurur - BIST 300 sekmesi icin.
    fetch_all_bist_tickers()'in aksine TUM piyasayi degil, sadece istenen
    kadarini ceker (n=300 icin 2 sayfa yeter, 628 degil) - hem daha az Yahoo
    istegi hem de sonuc zaten gunluk % sirali geldigi icin ekstra siralama
    gerekmez."""
    query = build_bist_query()
    symbols = []
    offset = 0
    try:
        while len(symbols) < n:
            page_size = min(SCREEN_PAGE_SIZE, n - len(symbols))
            result = yf.screen(query, sortField="percentchange", sortAsc=False, size=page_size, offset=offset)
            quotes = result.get("quotes", [])
            if not quotes:
                break
            symbols.extend(q["symbol"] for q in quotes if q.get("symbol"))
            offset += len(quotes)
            if offset >= result.get("total", 0):
                break
    except Exception:
        pass
    return symbols[:n]


def fetch_row(ticker: str):
    try:
        tk = yf.Ticker(ticker)
        # MACD(26,9) ve RSI/BB'nin saglikli yakinsamasi icin en az ~35 gunluk
        # mum gerekir - 2 ay yetersiz kalabildigi icin 6 aya cikarildi.
        hist = tk.history(period="6mo", interval="1d", auto_adjust=False)
        if hist.empty:
            return None

        closes = hist["Close"]
        last_close = float(closes.iloc[-1])

        # NOT: BIST (.IS) hisselerinde tk.fast_info donuk/bayat deger dondurebiliyor
        # (Yahoo'nun bu uc noktasi Istanbul borsasinda guncellenmiyor). Bunun yerine
        # tk.get_info()'nun regularMarketPrice/regularMarketPreviousClose alanlari
        # kullanilir, bu alanlar gercekten canli fiyati yansitiyor.
        info = tk.get_info()
        sector_en = info.get("sector")
        sector = SECTOR_TRANSLATE.get(sector_en, sector_en[:4].upper() if sector_en else "-")

        current_price = info.get("regularMarketPrice") or last_close
        prev_close = info.get("regularMarketPreviousClose") or (
            float(closes.iloc[-2]) if len(closes) >= 2 else last_close
        )

        # Gunluk degisim: canli fiyat vs Yahoo'nun canli "onceki kapanis" alani.
        # (closes.iloc[-2] gibi gecmis mum dizisinden indeksle secim yapmak,
        # Yahoo'nun gunluk mum verisinde bir gun eksik geldigi durumlarda yanlis
        # referans gunu secip degisimi ciddi sekilde yanlis hesaplayabiliyordu -
        # ornegin TKNKA'da 2 Eylul verisi eksikti, gercekte %8.4 olan gunluk
        # degisim bu yuzden %19.3 gorunmustu.)
        # Yahoo'nun kendi hesapladigi gunluk yuzde degisim degeri dogrudan
        # kullanilir (kendi pct_change hesabimiza gore tercih edilir).
        daily_pct = info.get("regularMarketChangePercent")
        if daily_pct is None:
            daily_pct = pct_change(current_price, prev_close)

        # Haftalik/aylik degisim, Yahoo'nun kendi haftalik/aylik mum verisindeki
        # (interval=1wk/1mo) donemin acilis fiyatina gore hesaplanir. Terminal
        # tablosunda gosterilmiyor, sadece web arayuzu icin.
        d5_pct = rolling_pct_change(closes, current_price, WEEKLY_LOOKBACK_DAYS)
        m1_pct = rolling_pct_change(closes, current_price, MONTHLY_LOOKBACK_DAYS)

        signal_5m = get_intraday_signal(tk)
        # Gunluk EMA kesisimi: zaten cekilmis olan gunluk kapanis serisi (closes)
        # uzerinden hesaplanir, ekstra Yahoo istegi gerekmez.
        signal_1d = ema_trend_signal(closes)

        # MACD/RSI/Bollinger: gunluk kapanis serisi (closes) uzerinden
        # hesaplanir, ekstra Yahoo istegi gerekmez.
        signal_macd = macd_signal(closes)
        rsi_val = rsi(closes)
        bb_percent = bollinger_percent_b(closes)
        adx_val = adx(hist)

        # Goreceli guc (RS): hissenin gunluk % degisimi ile BIST100 endeksinin
        # gunluk % degisimi arasindaki fark. Endeks +%4 oldugu bir gunde hisse
        # +%5 ise bu, endeksin duz oldugu bir gunde +%5 olmasindan cok daha az
        # anlamlidir - RS bu ayrimi yapar.
        benchmark_pct = get_benchmark_daily_pct()
        rel_strength = (
            daily_pct - benchmark_pct if (daily_pct == daily_pct and benchmark_pct is not None) else float("nan")
        )

        score_raw = confluence_score_raw(signal_1d, signal_5m, signal_macd, rsi_val, bb_percent, rel_strength)
        score = confluence_label(signal_1d, signal_5m, signal_macd, rsi_val, bb_percent, adx_val, rel_strength)

        # ATR bazli stop-loss/hedef: sinyalin yonune (AL/SAT) gore, hissenin
        # kendi oynakligina (ATR) olceklenmis gunluk (dar) ve swing (genis)
        # seviyeler. NOTR sinyalde net yon olmadigi icin (None, None) doner.
        atr_val = atr(hist)
        trade_direction = "AL" if score in ("AL", "GUCLU_AL") else "SAT" if score in ("SAT", "GUCLU_SAT") else None
        day_stop, day_target = stop_target_levels(current_price, atr_val, trade_direction, ATR_DAY_MULT, ATR_TARGET_R)
        swing_stop, swing_target = stop_target_levels(
            current_price, atr_val, trade_direction, ATR_SWING_MULT, ATR_TARGET_R
        )

        # Hacim orani: bugunku hacmin 10 gunluk ortalama hacme orani.
        # Yuksek oran (>1.5x gibi) sinyalin gercek katilimla desteklendigini gosterir.
        volume = info.get("regularMarketVolume")
        avg_volume = info.get("averageVolume10days")
        volume_ratio = (volume / avg_volume) if (volume and avg_volume) else float("nan")

        # Gun ici pozisyon: fiyatin gunun dip-zirve araligindaki yeri (%).
        # %100'e yakin = gun icinde alis baskisi guclu, %0'a yakin = satis baskisi guclu.
        day_high = info.get("regularMarketDayHigh")
        day_low = info.get("regularMarketDayLow")
        if day_high and day_low and day_high != day_low:
            day_range_pos = (current_price - day_low) / (day_high - day_low) * 100
        else:
            day_range_pos = float("nan")

        return {
            "ticker": ticker.removesuffix(".IS"),
            "sector": sector,
            "price": current_price,
            "daily": daily_pct,
            "d5": d5_pct,
            "m1": m1_pct,
            "signal_5m": signal_5m,
            "signal_1d": signal_1d,
            "signal_macd": signal_macd,
            "score": score,
            "score_raw": score_raw,
            "rsi": rsi_val,
            "bb_percent": bb_percent,
            "adx": adx_val,
            "rel_strength": rel_strength,
            "day_stop": day_stop,
            "day_target": day_target,
            "swing_stop": swing_stop,
            "swing_target": swing_target,
            "volume_ratio": volume_ratio,
            "day_range_pos": day_range_pos,
        }
    except Exception:
        return None


def fetch_all(tickers, max_workers: int = 12):
    """Sonucu, GONDERILEN ticker'a (orn. 'AKBNK.IS') gore anahtarlanmis bir
    sozluk olarak dondurur - row['ticker'] (goruntu icin '.IS' soneki
    temizlenmis hali, orn. 'AKBNK') ile KARISTIRILMAMALI. fetch_with_retry'nin
    basarili/basarisiz takibi bu ayrimi korumaya bagli (bkz. fetch_with_retry)."""
    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fetch_row, t): t for t in tickers}
        for future in as_completed(futures):
            ticker = futures[future]
            row = future.result()
            if row:
                results[ticker] = row
    return results


def fetch_with_retry(tickers, max_workers: int = 12, max_retries: int = MAX_RETRIES):
    """Basarisiz (None donen) hisseleri birkac kez tekrar dener,
    boylece gecici agi/istek hatalari yuzunden hisse eksik kalmaz.

    NOT: rows_by_ticker GONDERILEN ticker'a gore (orn. 'AKBNK.IS')
    anahtarlanir, row['ticker'] (goruntu icin soneksiz hali, 'AKBNK') ile
    DEGIL - onceden bu ikisi karistirilip pending listesi hicbir zaman
    kuculmuyordu, bu da basarili hisselerin bile her dongude MAX_RETRIES+1
    kez (gereksiz yere) tekrar cekilmesine yol aciyordu."""
    rows_by_ticker = {}
    pending = list(dict.fromkeys(tickers))  # sirali + tekrarsiz

    for _ in range(max_retries + 1):
        if not pending:
            break
        rows_by_ticker.update(fetch_all(pending, max_workers=max_workers))
        pending = [t for t in pending if t not in rows_by_ticker]

    return rows_by_ticker, pending


# Siralama onceligi: en guclu AL en ustte, en guclu SAT en altta - ayni
# skor grubunun icinde gunluk % buyukten kucuge sıralanir (web/terminal
# render sort_key'lerinde kullanilir).
SCORE_ORDER = {"GUCLU_AL": 0, "AL": 1, "NOTR": 2, "SAT": 3, "GUCLU_SAT": 4}


_last_logged_score = {}  # ticker -> log dosyasina en son yazilan skor


def load_device_tokens() -> dict:
    """Kayitli mobil cihaz push token'larini okur. Dosya yoksa (henuz kimse
    kayit olmadiysa) bos sozluk doner."""
    if not os.path.isfile(DEVICE_TOKENS_PATH):
        return {}
    try:
        with open(DEVICE_TOKENS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_device_tokens(tokens: dict) -> None:
    """Token sozlugunu atomik olarak yazar - once .tmp dosyasina yazip
    os.replace() ile degistirir, yazma sirasinda surec olursa (orn. servis
    restart) yarim/bozuk bir JSON dosyasi kalmasin diye."""
    tmp_path = DEVICE_TOKENS_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(tokens, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, DEVICE_TOKENS_PATH)


def register_device_token(token: str, platform: str) -> None:
    """Bir mobil cihazin push token'ini kaydeder/gunceller (upsert) - uygulama
    her acildiginda ayni token tekrar gelebilir, sorun degil."""
    tokens = load_device_tokens()
    tokens[token] = {
        "platform": platform,
        "registered_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_device_tokens(tokens)


def prune_device_tokens(dead_tokens: set) -> None:
    """FCM'in 'artik gecersiz' dedigi token'lari (uygulama silinmis, bildirim
    izni kaldirilmis vb.) kayitlardan temizler."""
    if not dead_tokens:
        return
    tokens = load_device_tokens()
    for t in dead_tokens:
        tokens.pop(t, None)
    save_device_tokens(tokens)


def _send_push_notifications(entries) -> None:
    """entries (log_signal_changes'in az once tespit ettigi GUCLU_AL/SAT
    gecisleri) icin kayitli tum cihazlara Firebase Cloud Messaging (FCM)
    uzerinden push bildirimi gonderir. Firebase henuz kurulmadiysa (paket
    kurulu degil ya da kimlik dosyasi yoksa) sessizce hicbir sey yapmaz -
    push, tarama dongusunun basarisi icin gerekli degildir."""
    if firebase_admin is None or not firebase_admin._apps:
        return
    tokens = load_device_tokens()
    if not tokens:
        return

    try:
        messages = []
        for entry in entries:
            label = "GUCLU AL" if entry["score"] == "GUCLU_AL" else "GUCLU SAT"
            title = f"{entry['ticker']} -> {label}"
            body = f"{entry['price']:.2f} TL ({entry['daily_pct']:+.2f}%)"
            for token in tokens:
                messages.append(
                    messaging.Message(
                        notification=messaging.Notification(title=title, body=body),
                        data={"ticker": entry["ticker"], "score": entry["score"]},
                        token=token,
                    )
                )
        if not messages:
            return

        response = messaging.send_each(messages)
        dead_tokens = set()
        for msg, result in zip(messages, response.responses):
            if not result.success and isinstance(
                result.exception, (messaging.UnregisteredError, messaging.SenderIdMismatchError)
            ):
                dead_tokens.add(msg.token)
        prune_device_tokens(dead_tokens)
    except Exception as e:
        print("Push bildirimi gonderilemedi:", e)


def log_signal_changes(rows):
    """Skoru GUCLU_AL/GUCLU_SAT'a DONEN (onceki dongude farkli olan) hisseleri
    signal_log tablosuna ekler. Her dongude ayni sinyali tekrar tekrar yazmaz -
    sadece durum degistiginde kaydeder, boylece log tablosu anlamli kalir ve
    her satir gercekten "yeni bir sinyal aninı" temsil eder."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    new_entries = []
    for row in rows:
        ticker = row["ticker"]
        score = row["score"]
        prev = _last_logged_score.get(ticker)
        if score != prev and score in ("GUCLU_AL", "GUCLU_SAT"):
            new_entries.append(
                {
                    "timestamp": now,
                    "ticker": ticker,
                    "score": score,
                    "price": row["price"],
                    "daily_pct": row["daily"],
                    "rsi": row["rsi"],
                    "adx": row["adx"],
                    "rel_strength": row["rel_strength"],
                }
            )
        _last_logged_score[ticker] = score

    if not new_entries:
        return

    with _DB_LOCK:
        conn = get_db_connection()
        try:
            conn.executemany(
                """INSERT INTO signal_log
                       (timestamp, ticker, score, price, daily_pct, rsi, adx, rel_strength, source)
                   VALUES (:timestamp, :ticker, :score, :price, :daily_pct, :rsi, :adx, :rel_strength, 'live')""",
                new_entries,
            )
            conn.commit()
        finally:
            conn.close()

    # DB yazimindan SONRA cagirilir - push gonderimi basarisiz olsa bile
    # tarihsel kayit her zaman yazilmis olur.
    _send_push_notifications(new_entries)


def log_score_history(rows):
    """log_signal_changes'in aksine SADECE GUCLU_AL/SAT'a donuste degil, HER
    dongude TUM hisseler icin bir satir yazar. Web arayuzundeki Skor Gecmisi
    panelinin 3 sekmesinin (gunluk ortalama / saatlik ortalama / 5 dakikalik
    ham veri) kaynagi budur - REFRESH_SECONDS zaten 5 dakika oldugu icin her
    dongu dogal olarak bir "5 dakikalik" ornek temsil eder."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entries = [
        {
            "timestamp": now,
            "ticker": row["ticker"],
            "score_raw": row["score_raw"],
            "score": row["score"],
            "price": row["price"],
        }
        for row in rows
    ]
    with _DB_LOCK:
        conn = get_db_connection()
        try:
            conn.executemany(
                """INSERT INTO score_history (timestamp, ticker, score_raw, score, price, source)
                   VALUES (:timestamp, :ticker, :score_raw, :score, :price, 'live')""",
                entries,
            )
            conn.commit()
        finally:
            conn.close()


def get_score_history(ticker: str, since_timestamp: str):
    """Verilen ticker icin since_timestamp'ten (dahil, 'YYYY-MM-DD HH:MM:SS')
    itibaren TUM skor gecmisi satirlarini (hem canli hem backfill kaynakli,
    'source' sutunuyla ayristirilir) zaman sirasiyla dondurur. Eskiden iki
    ayri bellek-ici index'ten (bkz. gecmis web_bistTop100.py) okunurdu; artik
    (ticker, timestamp) uzerinde indeksli tek bir SQLite sorgusu."""
    conn = get_db_connection()
    try:
        cur = conn.execute(
            """SELECT timestamp, score_raw, score, price, source
               FROM score_history
               WHERE ticker = ? AND timestamp >= ?
               ORDER BY timestamp ASC""",
            (ticker, since_timestamp),
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def run_paper_trading_cycle(rows) -> None:
    """Sanal para (paper trading) simulasyonunun ana dongusu -
    web_bistTop100.py: _scan_once() tarafindan HER tarama dongusunde
    (REFRESH_SECONDS) cagrilir, ayni rows (zaten taranmis, Yahoo'ya
    tekrar gidilmez) kullanilir. Cagiran taraf bunu kendi try/except'i
    icinde sarmali - burada beklenmeyen bir hata ana tarama/skor
    loglama akisini bozmamali.

    1) Acik pozisyonlari cikis kurallarina gore kapatir (stop/hedef/
       GUCLU_SAT'a gercek donus/sure asimi - ilk uyan kural kazanir).
    2) rows'taki YENI GUCLU_AL gecislerini (_paper_last_score ile
       karsilastirarak) tespit eder, acik slot kadarini sirayla satin
       alir.
    3) Her dongude bir equity (nakit + acik pozisyon degeri) satiri
       loglar."""
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    rows_by_ticker = {r["ticker"]: r for r in rows}

    with _DB_LOCK:
        conn = get_db_connection()
        try:
            cash = conn.execute("SELECT cash FROM paper_account WHERE id = 1").fetchone()["cash"]
            open_positions = [
                dict(r) for r in conn.execute("SELECT * FROM paper_trades WHERE status = 'open'").fetchall()
            ]

            closed_this_cycle = set()
            for pos in open_positions:
                row = rows_by_ticker.get(pos["ticker"])
                if row is None:
                    continue  # bu dongude veri gelmedi (delisted/gecici) - dokunma

                price = row["price"]
                exit_reason = None
                if pos["stop_price"] is not None and price <= pos["stop_price"]:
                    exit_reason = "stop"
                elif pos["target_price"] is not None and price >= pos["target_price"]:
                    exit_reason = "target"
                elif row["score"] == "GUCLU_SAT":
                    exit_reason = "signal_reversal"
                elif now_str >= pos["max_hold_until"]:
                    exit_reason = "time_stop"

                if exit_reason is not None:
                    pnl = (price - pos["entry_price"]) * pos["shares"]
                    conn.execute(
                        """UPDATE paper_trades
                           SET status='closed', exit_time=?, exit_price=?, exit_reason=?, pnl=?
                           WHERE id=?""",
                        (now_str, price, exit_reason, pnl, pos["id"]),
                    )
                    cash += price * pos["shares"]
                    closed_this_cycle.add(pos["ticker"])

            open_tickers = {p["ticker"] for p in open_positions} - closed_this_cycle
            open_count = len(open_tickers)

            # YENI GUCLU_AL gecislerini tespit et - rows zaten skor gucu +
            # gunluk % sirali (bkz. web_bistTop100.py: _scan_once()'taki
            # _sort_key), bu yuzden candidate_tickers de dogal olarak o
            # oncelik sirasinda.
            candidates = []
            for row in rows:
                ticker = row["ticker"]
                prev = _paper_last_score.get(ticker)
                if prev is not None and prev != "GUCLU_AL" and row["score"] == "GUCLU_AL":
                    candidates.append(row)
                _paper_last_score[ticker] = row["score"]

            free_slots = PAPER_MAX_POSITIONS - open_count
            for row in candidates:
                if free_slots <= 0:
                    break
                ticker = row["ticker"]
                if ticker in open_tickers or ticker in closed_this_cycle:
                    continue
                if row.get("swing_stop") is None or row.get("swing_target") is None:
                    continue  # ATR verisi yetersiz - guvenli bir stop/hedef yok
                price = row["price"]
                if price != price or price <= 0:
                    continue  # NaN/gecersiz fiyat (data glitch) - bu dongu atlanir

                allocation = min(PAPER_STARTING_BALANCE / PAPER_MAX_POSITIONS, cash)
                if allocation <= 0:
                    break
                shares = allocation / price
                max_hold_until = (now + timedelta(days=PAPER_MAX_HOLD_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
                conn.execute(
                    """INSERT INTO paper_trades
                           (ticker, status, entry_time, entry_price, shares, allocation,
                            stop_price, target_price, max_hold_until)
                       VALUES (?, 'open', ?, ?, ?, ?, ?, ?, ?)""",
                    (ticker, now_str, price, shares, allocation, row["swing_stop"], row["swing_target"], max_hold_until),
                )
                cash -= allocation
                open_tickers.add(ticker)
                free_slots -= 1

            conn.execute("UPDATE paper_account SET cash = ? WHERE id = 1", (cash,))

            positions_value = 0.0
            for r in conn.execute("SELECT ticker, shares FROM paper_trades WHERE status = 'open'").fetchall():
                row = rows_by_ticker.get(r["ticker"])
                if row is not None and row["price"] == row["price"]:  # NaN degilse
                    positions_value += row["price"] * r["shares"]
            conn.execute(
                "INSERT INTO paper_equity_history (timestamp, cash, positions_value, equity) VALUES (?, ?, ?, ?)",
                (now_str, cash, positions_value, cash + positions_value),
            )

            conn.commit()
        finally:
            conn.close()


def get_paper_positions():
    """Su an acik olan sanal pozisyonlari dondurur (giris bilgileriyle -
    guncel fiyat/K-Z hesaplamasi web_bistTop100.py'de canli rows ile
    birlestirilerek yapilir)."""
    conn = get_db_connection()
    try:
        cur = conn.execute("SELECT * FROM paper_trades WHERE status = 'open' ORDER BY entry_time DESC")
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_paper_trades(limit: int = 200):
    """Kapanmis sanal islemleri (en yeni once) dondurur."""
    conn = get_db_connection()
    try:
        cur = conn.execute(
            "SELECT * FROM paper_trades WHERE status = 'closed' ORDER BY exit_time DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_paper_equity_history(since_timestamp: str):
    """since_timestamp'ten itibaren equity (nakit + acik pozisyon degeri)
    gecmisini dondurur - Skor Gecmisi'ndeki gunluk/saatlik/5dk sekmeleriyle
    ayni sekilde (ham veri, agregasyon okuma anında) grafik/tablo icin."""
    conn = get_db_connection()
    try:
        cur = conn.execute(
            "SELECT timestamp, cash, positions_value, equity FROM paper_equity_history "
            "WHERE timestamp >= ? ORDER BY timestamp ASC",
            (since_timestamp,),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_paper_summary():
    """Sanal hesabin o anki ozetini dondurur: nakit, acik pozisyon degeri,
    toplam equity, gerceklesmis/gerceklesmemis K-Z, kazanma orani. Acik
    pozisyonlarin guncel degeri icin en son equity satirini kullanir (
    run_paper_trading_cycle her dongude bunu zaten gunceller)."""
    conn = get_db_connection()
    try:
        account = conn.execute("SELECT cash, starting_balance FROM paper_account WHERE id = 1").fetchone()
        last_equity = conn.execute(
            "SELECT equity, positions_value FROM paper_equity_history ORDER BY id DESC LIMIT 1"
        ).fetchone()
        closed = conn.execute("SELECT pnl FROM paper_trades WHERE status = 'closed'").fetchall()
        open_count = conn.execute("SELECT COUNT(*) AS c FROM paper_trades WHERE status = 'open'").fetchone()["c"]

        realized_pnl = sum(r["pnl"] for r in closed if r["pnl"] is not None)
        equity = last_equity["equity"] if last_equity else account["cash"]
        positions_value = last_equity["positions_value"] if last_equity else 0.0
        wins = sum(1 for r in closed if r["pnl"] is not None and r["pnl"] > 0)

        open_allocation = conn.execute(
            "SELECT COALESCE(SUM(allocation), 0) AS total FROM paper_trades WHERE status = 'open'"
        ).fetchone()["total"]
        unrealized_pnl = positions_value - open_allocation

        return {
            "cash": account["cash"],
            "positions_value": positions_value,
            "equity": equity,
            "starting_balance": account["starting_balance"],
            "total_pnl": equity - account["starting_balance"],
            "realized_pnl": realized_pnl,
            "unrealized_pnl": unrealized_pnl,
            "open_positions": open_count,
            "closed_trades": len(closed),
            "win_rate": (wins / len(closed) * 100.0) if closed else None,
        }
    finally:
        conn.close()

