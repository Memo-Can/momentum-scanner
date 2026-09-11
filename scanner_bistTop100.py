#!/usr/bin/env python3
"""BIST is katmani (sunum icermez). BIST genelinde tum piyasayi (Yahoo
Finance taramasi, region=tr, filtresiz) gunluk yuzde degisime gore siralayip
ilk TOP_N=100 hisseyi secer; her hisse icin EMA/MACD/RSI/Bollinger/ADX/ATR
indikatorlerini hesaplayip tek bir Skor'a (-6..+6, GUCLU_AL..GUCLU_SAT)
birlestirir. Bu modul dogrudan calistirilmaz - terminal_bistTop100.py
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

import csv
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

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
TOP_N = 100
CANDIDATE_BUFFER = 30  # eksik hisse kalmamasi icin ihtiyactan fazla aday cekilir
MAX_RETRIES = 2

# GUCLU_AL/GUCLU_SAT sinyallerinin gecmisini kaydeder - zamanla bu kayitlara
# bakip Skor'un gercekte ise yarayip yaramadigini (sinyalden sonra fiyat ne
# oldu) kendi verinizle degerlendirebilirsiniz. Sadece durum DEGISTIGINDE
# yazilir (her dongude tekrar tekrar degil), boylece dosya sismez.
SIGNAL_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signal_log_bist.csv")
SCORE_HISTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "score_history_bist.csv")
DEVICE_TOKENS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "device_tokens_bist.json")
SIGNAL_LOG_FIELDS = ["timestamp", "ticker", "score", "price", "daily_pct", "rsi", "adx", "rel_strength"]
SCORE_HISTORY_FIELDS = ["timestamp", "ticker", "score_raw", "score", "price"]

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


def adx(hist, period: int = ADX_PERIOD) -> float:
    """Wilder ADX(14) degerini dondurur (0-100). ADX yonu degil trendin
    GUCUNU olcer: <20 zayif/yatay piyasa (sinyaller gurultulu olabilir),
    >=25 gercek/guclu trend sayilir. Skor'un GUCLU AL/SAT etiketini
    onaylamak icin guven filtresi olarak kullanilir."""
    if hist is None or len(hist) < period * 2:
        return float("nan")
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
    adx_val = dx.ewm(alpha=1 / period, adjust=False).mean().iloc[-1]
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
    # Yahoo'nun BIST (Borsa Istanbul) icin tum piyasayi tarayan sorgusu.
    # region="tr" tum BIST hisselerini kapsar, herhangi bir esik/filtre yoktur.
    return yf.EquityQuery("eq", ["region", "tr"])


def fetch_bist_tickers(top_n: int):
    try:
        query = build_bist_query()
        result = yf.screen(query, sortField="percentchange", sortAsc=False, size=top_n)
        quotes = result.get("quotes", [])
        return [q["symbol"] for q in quotes if q.get("symbol")][:top_n]
    except Exception:
        return []


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
    SIGNAL_LOG_PATH'e ekler. Her dongude ayni sinyali tekrar tekrar yazmaz -
    sadece durum degistiginde kaydeder, boylece log dosyasi anlamli kalir ve
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
    file_exists = os.path.isfile(SIGNAL_LOG_PATH)
    with open(SIGNAL_LOG_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SIGNAL_LOG_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerows(new_entries)

    # CSV yazimindan SONRA cagirilir - push gonderimi basarisiz olsa bile
    # CSV log (tarihsel kayit) her zaman yazilmis olur.
    _send_push_notifications(new_entries)


def log_score_history(rows):
    """log_signal_changes'in aksine SADECE GUCLU_AL/SAT'a donuste degil, HER
    dongude TUM hisseler icin bir satir yazar. Web arayuzundeki Skor Gecmisi
    panelinin 3 sekmesinin (gunluk ortalama / saatlik ortalama / 5 dakikalik
    ham veri) kaynagi budur - REFRESH_SECONDS zaten 5 dakika oldugu icin her
    dongu dogal olarak bir "5 dakikalik" ornek temsil eder."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    file_exists = os.path.isfile(SCORE_HISTORY_PATH)
    with open(SCORE_HISTORY_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SCORE_HISTORY_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerows(
            {
                "timestamp": now,
                "ticker": row["ticker"],
                "score_raw": row["score_raw"],
                "score": row["score"],
                "price": row["price"],
            }
            for row in rows
        )


