#!/usr/bin/env python3
"""BIST genelinde gunun en cok kazandiran hisseleri gunluk yuzde degisime
gore siralayip listeler (toplam TOP_N=100 hisse). Hisse listesi iki kaynaktan
olusur:
1) BIST30 endeksindeki 30 hisse - GUNLUK % NE OLURSA OLSUN her zaman listede
   yer alir (buyuk/likit hisseler nadiren %3 esigini gectigi icin salt
   "en cok kazandiran" taramasinda kaybolabiliyorlardi, bu yuzden sabitlendi).
2) Yahoo Finance'in ozel taramasindan (percentchange > 3, region = tr)
   dinamik olarak cekilen, BIST30 disindaki en cok kazandiran ek hisseler -
   toplam goruntu sayisi TOP_N'e tamamlanana kadar (100-30=70 ek hisse).

Her REFRESH_SECONDS (300sn / 5dk) saniyede bir hem tarayici listesi hem de getiriler
Yahoo Finance'ten yeniden cekilip terminal yenilenir.

Sutunlar:
  #        Siralamadaki yeri: once Skor gucu (GUCLU AL en ustte, GUCLU SAT en
           altta), ayni skor grubu icinde gunluk getiriye gore buyukten kucuge
  Skor     1d/5m EMA, MACD, RSI, BB% ve RS'nin tek bir konfluens skoruna
           (-6..+6) birlestirilmis hali: GUCLU AL(▲▲)/AL(▲)/NOTR(–)/SAT(▼)/
           GUCLU SAT(▼▼). 6 ayri sinyale tek tek bakmak yerine tek bakista
           netlik saglar. ADX(14) ayri bir sutun olarak gosterilmez, GUCLU
           AL/SAT etiketini teyit eden bir guven filtresi olarak kullanilir:
           ADX<20 (zayif/yatay piyasa) ise "guclu" etiket otomatik normal
           AL/SAT'a duser.
  1d       Gunluk mumda EMA9/EMA21'e gore SUREKLI trend durumu (AL/SAT/NOTR)
  5m       5 dakikalik mumda EMA9/EMA21'e gore SUREKLI trend durumu
           (1d ile ayni mantik, farkli zaman dilimi; ikisi de kesisim aninda
           degil, EMA9'un EMA21'e gore o anki konumuna gore surekli AL/SAT
           gosterir). Renk: AL=yesil, SAT=kirmizi, NOTR=sari - satirin genel
           renginden (Gunluk +/-'ye gore) bagimsizdir.
  Sektor   Hissenin kisa sektor kodu. Bu dosyada hisse listesi dinamik oldugu
           icin sabit bir harita yerine Yahoo'nun canli "sector" alani
           (SECTOR_TRANSLATE ile kisa koda cevrilir) kullanilir.
  MACD     MACD histogramina (12,26,9) gore SUREKLI AL/SAT/NOTR (histogram
           farki > 0 = AL). EMA9/21'e ek momentum-gucu teyidi olarak okunur.
  RSI      Wilder RSI(14) degeri (0-100). >=70 asiri alim, <=30 asiri satim
           olarak yorumlanabilir; momentum taramasinda 50 uzeri boga teyidi.
  BB%      Bollinger Bantlari (20,2sigma) icindeki konum (%). 100%=ust bant,
           0%=alt bant. Bandin disina cikan (>100%/<0%) deger, hacimle
           birlikteyse aşırı alım degil guclu kirilim/momentum sayilir.
  Fiyat    Canli fiyat, TL (Yahoo'nun regularMarketPrice alani)
  Hacim    Bugunku islem hacminin 10 gunluk ortalama hacme orani (orn. 2.3x).
           Yuksek oran, fiyat hareketinin gercek katilimla desteklendigini gosterir.
  GunPoz   Fiyatin gunun dip-zirve araligindaki yeri (%). %100=gunun zirvesi
           (alici baskin), %0=gunun dibi (satici baskin).
  RS       Goreceli guc: hissenin gunluk % degisimi eksi BIST100 endeksinin
           (XU100.IS) gunluk % degisimi. Endeks +%4 oldugu bir gunde hissenin
           +%5 olmasi, endeksin duz oldugu bir gunde +%5 olmasindan cok daha
           az anlamlidir - RS bu ayrimi sayisallastirir. |RS| < 0.5 NOTR.
  Gunluk   Canli fiyat ile Yahoo'nun canli "onceki kapanis" alani arasindaki
           yuzde fark (satir rengi buna gore yesil/kirmizi olur, liste bu
           sutuna gore buyukten kucuge siralanir)
  GunStop/Hedef  ATR(14)*1.5 mesafeli, sinyal yonune (Skor'un AL/SAT egilimine)
           gore gunluk (day trade) stop-loss/hedef fiyat cifti. Hedef mesafesi
           stop mesafesinin 2 kati (risk:odul ~1:2). Skor NOTR ise "-".
  SwStop/Hedef  Ayni mantik, ATR(14)*3.0 mesafeli - birkac gun/hafta tasinacak
           swing pozisyonlar icin daha genis stop/hedef.

Onbellek: Sadece 5dk sinyali INTRADAY_CACHE_SECONDS (5 dakika) boyunca,
BIST100 endeksinin gunluk degisimi ise REFRESH_SECONDS'a yakin bir sure
onbellekten dondurulur - boylece Yahoo'ya gereksiz istek atilmaz.

Ses: Her basarili yenilemede (REFRESH_SECONDS'ta bir) tek bir bip sesi
(al_beep.wav) calinir - sinyale bagli degildir.

Sinyal gecmisi: Skoru GUCLU_AL/GUCLU_SAT'a DONEN hisseler signal_log_bist.csv
dosyasina eklenir (sadece durum degistiginde, spam onlenir). Zamanla bu
kayitlara bakip Skor'un gercekte ise yarayip yaramadigini (sinyalden sonra
fiyat ne oldu) kendi verinizle degerlendirebilirsiniz.
"""

import csv
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import pandas as pd
import yfinance as yf

REFRESH_SECONDS = 300  # 5 dakika
TOP_N = 100
CANDIDATE_BUFFER = 30  # eksik hisse kalmamasi icin ihtiyactan fazla aday cekilir
MAX_RETRIES = 2
MIN_PRICE = 1  # kurusun altindaki cop kagitlari elemek icin fiyat tabani (TL)
INTRADAY_CACHE_SECONDS = 5 * 60  # 5dk mum verisi 5 dakikada bir yenilenir

SOUND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sounds")
BEEP_SOUND = os.path.join(SOUND_DIR, "al_beep.wav")

# GUCLU_AL/GUCLU_SAT sinyallerinin gecmisini kaydeder - zamanla bu kayitlara
# bakip Skor'un gercekte ise yarayip yaramadigini (sinyalden sonra fiyat ne
# oldu) kendi verinizle degerlendirebilirsiniz. Sadece durum DEGISTIGINDE
# yazilir (her dongude tekrar tekrar degil), boylece dosya sismez.
SIGNAL_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signal_log_bist.csv")
SIGNAL_LOG_FIELDS = ["timestamp", "ticker", "score", "price", "daily_pct", "rsi", "adx", "rel_strength"]

# BIST30 endeksindeki hisseler - gunluk % ne olursa olsun listede daima
# yer alirlar (bkz. dosya basindaki aciklama).
BIST30_TICKERS = [
    "AKBNK", "ALARK", "ARCLK", "ASELS", "BIMAS", "EKGYO", "ENKAI", "EREGL",
    "FROTO", "GARAN", "HALKB", "ISCTR", "KCHOL", "KONTR", "KRDMD", "MGROS",
    "OYAKC", "PETKM", "PGSUS", "SAHOL", "SASA", "SISE", "TAVHL", "TCELL",
    "THYAO", "TOASO", "TTKOM", "TUPRS", "VAKBN", "YKBNK",
]
BIST30_TICKERS = [f"{t}.IS" for t in BIST30_TICKERS]


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


_intraday_signal_cache = {}  # ticker -> (fetched_at, signal)
_benchmark_cache = {}  # "pct" -> (fetched_at, daily_pct)


def get_benchmark_daily_pct():
    """BIST100 endeksinin gunluk yuzde degisimini REFRESH_SECONDS'a yakin bir
    sure onbellekten dondurur - her hisse icin ayri ayri degil, dongu basina
    bir kez cekilir. Hisselerin gercekten piyasayla birlikte mi surukendigi
    yoksa piyasadan bagimsiz mi guclu oldugu (RS) bunun uzerinden hesaplanir."""
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


def get_intraday_signal(tk, ticker: str) -> str:
    """5dk mum uzerindeki surekli EMA9/EMA21 trend durumunu (kesisim ani degil)
    INTRADAY_CACHE_SECONDS boyunca onbellekten dondurur; 5dk'lik mumlar zaten
    5 dakikada bir olustugu icin daha sik cekmenin anlami yok."""
    cached = _intraday_signal_cache.get(ticker)
    if cached and time.time() - cached[0] < INTRADAY_CACHE_SECONDS:
        return cached[1]
    intraday = tk.history(period="5d", interval="5m", auto_adjust=False)
    signal = ema_trend_signal(intraday["Close"] if not intraday.empty else None)
    _intraday_signal_cache[ticker] = (time.time(), signal)
    return signal


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


def build_gainers_query():
    # Yahoo'nun BIST (Borsa Istanbul) icin tum piyasayi tarayan sorgusu.
    # region="tr" tum BIST hisselerini kapsar, sabit bir listeyle sinirli degildir.
    # DENEME: hacim/fiyat filtreleri (dayvolume>15000, intradayprice>=MIN_PRICE)
    # gecici olarak kaldirildi.
    return yf.EquityQuery(
        "and",
        [
            yf.EquityQuery("gt", ["percentchange", 3]),
            yf.EquityQuery("eq", ["region", "tr"]),
        ],
    )


def fetch_top_gainer_tickers(top_n: int):
    try:
        query = build_gainers_query()
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

        signal_5m = get_intraday_signal(tk, ticker)
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
            "signal_5m": signal_5m,
            "signal_1d": signal_1d,
            "signal_macd": signal_macd,
            "score": score,
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


GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
BOLD_GREEN = "\033[1;32m"
BOLD_RED = "\033[1;31m"
RESET = "\033[0m"

SIGNAL_SYMBOLS = {"AL": "▲", "SAT": "▼", "NOTR": "–"}
SIGNAL_COLORS = {"AL": GREEN, "SAT": RED, "NOTR": YELLOW}

SCORE_SYMBOLS = {"GUCLU_AL": "▲▲", "AL": "▲", "NOTR": "–", "SAT": "▼", "GUCLU_SAT": "▼▼"}
SCORE_COLORS = {
    "GUCLU_AL": BOLD_GREEN,
    "AL": GREEN,
    "NOTR": YELLOW,
    "SAT": RED,
    "GUCLU_SAT": BOLD_RED,
}

# Siralama onceligi: en guclu AL en ustte, en guclu SAT en altta - ayni
# skor grubunun icinde gunluk % buyukten kucuge sıralanir (asagida sort_key).
SCORE_ORDER = {"GUCLU_AL": 0, "AL": 1, "NOTR": 2, "SAT": 3, "GUCLU_SAT": 4}


def play_beep(path: str):
    try:
        subprocess.Popen(["aplay", "-q", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def play_refresh_beep():
    play_beep(BEEP_SOUND)


_last_logged_score = {}  # ticker -> log dosyasina en son yazilan skor


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


def fmt_pct(val: float) -> str:
    if val != val:  # NaN check
        return "   -   "
    sign = "+" if val >= 0 else ""
    return f"{sign}{val:.2f}%"


def fmt_ratio(val: float) -> str:
    if val != val:  # NaN check
        return "  -  "
    return f"{val:.1f}x"


def fmt_range_pos(val: float) -> str:
    if val != val:  # NaN check
        return "  -  "
    return f"{val:.0f}%"


def fmt_rsi(val: float) -> str:
    if val != val:  # NaN check
        return " - "
    return f"{val:.0f}"


def fmt_stop_target(stop, target) -> str:
    """Stop-loss/hedef ciftini 'stop/hedef' seklinde tek bir kompakt metne
    cevirir. Yon belirsizse (NOTR sinyal) ikisi de None olur, '-' gosterilir."""
    if stop is None or target is None:
        return "-"
    return f"{stop:.2f}/{target:.2f}"


def render(rows, missing_count: int = 0):
    use_color = sys.stdout.isatty()
    if use_color:
        os.system("cls" if os.name == "nt" else "clear")
    def _sort_key(row):
        daily = row["daily"]
        daily_sort = -daily if daily == daily else float("inf")  # NaN'lari sona at
        return (SCORE_ORDER.get(row["score"], 2), daily_sort)

    rows_sorted = sorted(rows, key=_sort_key)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"BIST30 + En Cok Kazandiranlar - Getiri Tablosu ({len(rows)} hisse)   (guncelleme: {now})")
    print(f"Kaynak: BIST30 (daima dahil) + ozel tarama (fiyat/hacim filtresi YOK - DENEME)   |  Siralama: Once Skor gucu (GUCLU AL->AL->NOTR->SAT->GUCLU SAT), ayni grup icinde gunluk % (yuksekten dusuge)")
    print(f"Sinyal: EMA{EMA_FAST}/EMA{EMA_SLOW} (1d/5dk) + MACD({MACD_FAST},{MACD_SLOW},{MACD_SIGNAL}) + RSI({RSI_PERIOD}) + BB({BB_PERIOD},{BB_STD}sigma)   |  her {REFRESH_SECONDS} sn'de bir yenilenir")
    if missing_count:
        print(f"Uyari: {missing_count} BIST30 hissesi icin veri alinamadi.")
    print()

    header = (
        f"{'#':>2} {'Skor':^4} {'1d':^2} {'5m':^2} {'MACD':^4} {'Hisse':<5} {'Sektor':<6} {'Fiyat':>6} "
        f"{'Hacim':>5} {'GunPoz':>6} {'RSI':>3} {'BB%':>4} {'RS':>8} {'Gunluk':>8} "
        f"{'GunStop/Hedef':>13} {'SwStop/Hedef':>13}"
    )
    print(header)
    print("-" * len(header))

    for i, row in enumerate(rows_sorted, start=1):
        symbol_score = f"{SCORE_SYMBOLS.get(row['score'], row['score']):^4}"
        symbol_1d = f"{SIGNAL_SYMBOLS.get(row['signal_1d'], row['signal_1d']):^2}"
        symbol_5m = f"{SIGNAL_SYMBOLS.get(row['signal_5m'], row['signal_5m']):^2}"
        symbol_macd = f"{SIGNAL_SYMBOLS.get(row['signal_macd'], row['signal_macd']):^4}"
        idx = f"{i:>2}"
        prefix = (
            f"{row['ticker']:<5} {row['sector']:<6} {row['price']:>6.2f} "
            f"{fmt_ratio(row['volume_ratio']):>5} {fmt_range_pos(row['day_range_pos']):>6} "
            f"{fmt_rsi(row['rsi']):>3} {fmt_range_pos(row['bb_percent']):>4} "
            f"{fmt_pct(row['rel_strength']):>8} {fmt_pct(row['daily']):>8} "
            f"{fmt_stop_target(row['day_stop'], row['day_target']):>13} "
            f"{fmt_stop_target(row['swing_stop'], row['swing_target']):>13}"
        )
        if use_color:
            row_color = GREEN if row["daily"] >= 0 else RED
            idx = f"{row_color}{idx}{RESET}"
            symbol_score = f"{SCORE_COLORS.get(row['score'], YELLOW)}{symbol_score}{RESET}"
            symbol_1d = f"{SIGNAL_COLORS.get(row['signal_1d'], YELLOW)}{symbol_1d}{RESET}"
            symbol_5m = f"{SIGNAL_COLORS.get(row['signal_5m'], YELLOW)}{symbol_5m}{RESET}"
            symbol_macd = f"{SIGNAL_COLORS.get(row['signal_macd'], YELLOW)}{symbol_macd}{RESET}"
            prefix = f"{row_color}{prefix}{RESET}"
        print(f"{idx} {symbol_score} {symbol_1d} {symbol_5m} {symbol_macd} {prefix}")

    print(f"\nCikmak icin CTRL+C")
    sys.stdout.flush()


def main():
    bist30_symbols = {t.removesuffix(".IS") for t in BIST30_TICKERS}
    while True:
        gainer_tickers = fetch_top_gainer_tickers(TOP_N + CANDIDATE_BUFFER)
        # BIST30 her zaman aday havuzunda - gunluk % esigini gecmese bile
        # (ornegin buyuk/likit bir hisse) listeden hic dislanmasin diye.
        candidate_tickers = list(dict.fromkeys(BIST30_TICKERS + gainer_tickers))
        rows_by_ticker, still_missing = fetch_with_retry(candidate_tickers) if candidate_tickers else ({}, [])

        bist30_rows = [r for r in rows_by_ticker.values() if r["ticker"] in bist30_symbols]
        other_rows = [r for r in rows_by_ticker.values() if r["ticker"] not in bist30_symbols]
        other_rows_sorted = sorted(other_rows, key=lambda r: r["daily"], reverse=True)

        # BIST30 her zaman dahil; kalan yerler en cok kazandiran diger hisselerle doldurulur.
        extra_slots = max(0, TOP_N - len(bist30_rows))
        top_rows = bist30_rows + other_rows_sorted[:extra_slots]
        missing_count = max(0, len(BIST30_TICKERS) - len(bist30_rows))

        if top_rows:
            render(top_rows, missing_count=missing_count)
            play_refresh_beep()
            log_signal_changes(top_rows)
        else:
            print("Veri cekilemedi, tekrar denenecek...")
        try:
            time.sleep(REFRESH_SECONDS)
        except KeyboardInterrupt:
            sys.exit(0)


if __name__ == "__main__":
    main()
