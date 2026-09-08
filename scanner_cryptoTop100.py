#!/usr/bin/env python3
"""Piyasa degerine gore en buyuk ~100 kripto parayi gunluk yuzde degisime gore
siralayip listeler. Calisma mantigi scanner_bistTop100/scanner_usTop100 ile
buyuk olcude ayni (EMA/MACD/RSI/BB/ADX/Skor motoru birebir aynidir), ancak
hisse listesi TAMAMEN SABITTIR (CRYPTO_TICKERS) - dinamik bir "en cok
kazandiranlar" taraması YOKTUR.

NEDEN SABIT LISTE: BIST/ABD dosyalarinda kullanilan yf.screen() (Yahoo'nun
"en cok kazandiran" taramasi) sadece hisse senedi (equity) varlik sinifini
destekliyor; kripto icin ne EquityQuery'de bir "region" secenegi ne de
yfinance'in hazir taramalarinda ("PREDEFINED_SCREENER_QUERIES") bir kripto
sorgusu var (denendi, ikisi de calismiyor/404 donuyor). Bu yuzden BIST30/
TOP_US_TICKERS gibi "sabit + dinamik tamamlama" hibrit yapisi yerine, burada
sadece sabit bir liste izleniyor. Pratikte bu buyuk bir kayip degil: kripto
piyasasindaki ilgi/hacim zaten buyuk olcude ust siradaki birkac düzine
coin'de yogunlasiyor.

7/24 PIYASA: Kripto surekli islem gordugu icin (marketState hep "REGULAR"),
BIST/ABD'de oldugu gibi piyasa acilis/kapanis saatiyle ilgili hicbir mantik
yok - scanner her REFRESH_SECONDS'ta ayni sekilde calisir.

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
  MACD     MACD histogramina (12,26,9) gore SUREKLI AL/SAT/NOTR (histogram
           farki > 0 = AL). EMA9/21'e ek momentum-gucu teyidi olarak okunur.
  RSI      Wilder RSI(14) degeri (0-100). >=70 asiri alim, <=30 asiri satim
           olarak yorumlanabilir; momentum taramasinda 50 uzeri boga teyidi.
  BB%      Bollinger Bantlari (20,2sigma) icindeki konum (%). 100%=ust bant,
           0%=alt bant. Bandin disina cikan (>100%/<0%) deger, hacimle
           birlikteyse aşırı alım degil guclu kirilim/momentum sayilir.
  Fiyat    Canli fiyat, $ (Yahoo'nun regularMarketPrice alani)
  Hacim    Bugunku islem hacminin 10 gunluk ortalama hacme orani (orn. 2.3x).
           Yuksek oran, fiyat hareketinin gercek katilimla desteklendigini gosterir.
  GunPoz   Fiyatin gunun dip-zirve araligindaki yeri (%). %100=gunun zirvesi
           (alici baskin), %0=gunun dibi (satici baskin).
  Gunluk   Canli fiyat ile Yahoo'nun canli "onceki kapanis" alani arasindaki
           yuzde fark (satir rengi buna gore yesil/kirmizi olur, liste bu
           sutuna gore buyukten kucuge siralanir)
  RS       Goreceli guc: coin'in gunluk % degisimi eksi BTC'nin gunluk %
           degisimi. Kripto piyasasinda BTC "piyasanin kendisi" gibi
           davranir - altcoin'in BTC'den bagimsiz gercekten guclu mu, yoksa
           sadece BTC ile birlikte mi surukendigini ayirt eder. BTC'nin
           kendisi icin RS her zaman ~0 (kendisiyle kiyaslanir). |RS| < 0.5
           NOTR.
  GunStop/Hedef  ATR(14)*1.5 mesafeli, sinyal yonune (Skor'un AL/SAT egilimine)
           gore gunluk (day trade) stop-loss/hedef fiyat cifti. Hedef mesafesi
           stop mesafesinin 2 kati (risk:odul ~1:2). Skor NOTR ise "-".
  SwStop/Hedef  Ayni mantik, ATR(14)*3.0 mesafeli - birkac gun/hafta tasinacak
           swing pozisyonlar icin daha genis stop/hedef.

Onbellek: 5dk sinyali ve haftalik/aylik her REFRESH_SECONDS'ta Yahoo'dan
yeniden cekilir (onbellek yok). Sadece BTC karsilastirmasi (RS icin) dongu
basina onbelleklenir - yoksa her coin icin ayri ayri cekilip 100 kat
gereksiz istege yol acardi.

Ses: Her basarili yenilemede (REFRESH_SECONDS'ta bir) tek bir bip sesi
(al_beep.wav) calinir - sinyale bagli degildir.

Sinyal gecmisi: Skoru GUCLU_AL/GUCLU_SAT'a DONEN coin'ler signal_log_crypto.csv
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
MAX_RETRIES = 2

SOUND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sounds")
BEEP_SOUND = os.path.join(SOUND_DIR, "al_beep.wav")

# GUCLU_AL/GUCLU_SAT sinyallerinin gecmisini kaydeder - zamanla bu kayitlara
# bakip Skor'un gercekte ise yarayip yaramadigini (sinyalden sonra fiyat ne
# oldu) kendi verinizle degerlendirebilirsiniz. Sadece durum DEGISTIGINDE
# yazilir (her dongude tekrar tekrar degil), boylece dosya sismez.
SIGNAL_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signal_log_crypto.csv")
SIGNAL_LOG_FIELDS = ["timestamp", "ticker", "score", "price", "daily_pct", "rsi", "adx", "rel_strength"]
SCORE_HISTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "score_history_crypto.csv")
SCORE_HISTORY_FIELDS = ["timestamp", "ticker", "score_raw", "score", "price"]

# Piyasa degerine gore en buyuk ~100 kripto para (Yahoo Finance ticker formati).
# Bazi coin'ler Yahoo'da numarali sonek gerektiriyor (ayni sembol koduna sahip
# baska bir varlikla karismasin diye) - bunlarin temiz gorunum adi
# DISPLAY_OVERRIDES ile ayarlanir. Liste yfinance uzerinden tek tek/toplu
# dogrulanarak olusturuldu (gecersiz/bulunamayan tikerlar elendi: eski MATIC/
# POL, UNI, APT, GRT, SUI, PEPE, IMX gibi bazilari duz sembolle calismadigi
# icin numarali sonekli halleriyle eklendi).
CRYPTO_TICKERS = [
    "BTC-USD", "ETH-USD", "XRP-USD", "BNB-USD", "SOL-USD",
    "DOGE-USD", "ADA-USD", "TRX-USD", "AVAX-USD", "SHIB-USD",
    "DOT-USD", "LINK-USD", "BCH-USD", "NEAR-USD", "LTC-USD",
    "ICP-USD", "ETC-USD", "XLM-USD", "ATOM-USD", "XMR-USD",
    "FIL-USD", "HBAR-USD", "ARB-USD", "VET-USD", "MKR-USD",
    "OP-USD", "INJ-USD", "ALGO-USD", "AAVE-USD", "EGLD-USD",
    "SAND-USD", "MANA-USD", "AXS-USD", "XTZ-USD", "EOS-USD",
    "THETA-USD", "FLOW-USD", "KAS-USD", "RENDER-USD", "RUNE-USD",
    "CRV-USD", "SNX-USD", "ZEC-USD", "TON-USD", "PEPE24478-USD",
    "UNI7083-USD", "APT21794-USD", "GRT6719-USD", "SUI20947-USD", "IMX10603-USD",
    "WIF-USD", "BONK-USD", "LDO-USD", "QNT-USD", "TIA-USD",
    "SEI-USD", "ORDI-USD", "WLD-USD", "FET-USD", "GALA-USD",
    "AR-USD", "KCS-USD", "CFX-USD", "NEO-USD", "IOTA-USD",
    "DYDX-USD", "MINA-USD", "ROSE-USD", "1INCH-USD", "YFI-USD",
    "BAT-USD", "WAVES-USD", "KAVA-USD", "LRC-USD", "ENS-USD",
    "CVX-USD", "PYTH-USD", "JUP-USD", "STRK-USD", "AKT-USD",
    "CAKE-USD", "TWT-USD", "XEC-USD", "HNT-USD", "DASH-USD",
    "ZRX-USD", "BSV-USD", "GNO-USD", "SUSHI-USD", "BAL-USD",
    "CELO-USD", "QTUM-USD", "FLR-USD", "XDC-USD", "IOTX-USD",
    "RVN-USD", "DGB-USD", "OSMO-USD", "SKL-USD", "STORJ-USD",
]
TOP_N = len(CRYPTO_TICKERS)

# Yahoo'da numarali sonekle bulunan coin'lerin ekranda temiz gorunmesi icin.
DISPLAY_OVERRIDES = {
    "PEPE24478-USD": "PEPE",
    "UNI7083-USD": "UNI",
    "APT21794-USD": "APT",
    "GRT6719-USD": "GRT",
    "SUI20947-USD": "SUI",
    "IMX10603-USD": "IMX",
}


def display_symbol(ticker: str) -> str:
    return DISPLAY_OVERRIDES.get(ticker, ticker.removesuffix("-USD"))


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
BENCHMARK_TICKER = "BTC-USD"  # BTC "piyasanin kendisi" gibi - RS bazi
RS_THRESHOLD = 0.5  # bu esigin altindaki fark (yuzde puan) NOTR sayilir
ATR_PERIOD = 14
ATR_DAY_MULT = 1.5  # gunluk (day trade) stop mesafesi = ATR * bu katsayi (dar)
ATR_SWING_MULT = 3.0  # swing stop mesafesi = ATR * bu katsayi (genis)
ATR_TARGET_R = 2.0  # hedef mesafesi = stop mesafesi * bu katsayi (risk:odul ~1:2)
WEEKLY_LOOKBACK_DAYS = 5  # "5D" - kayan pencere, takvim haftasi degil
MONTHLY_LOOKBACK_DAYS = 21  # "1M" - ~1 aylik islem gunu sayisi, kayan pencere


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
    """BTC'nin gunluk yuzde degisimini REFRESH_SECONDS'a yakin bir sure
    onbellekten dondurur - her coin icin ayri ayri degil, dongu basina bir
    kez cekilir (onbellek olmadan, 100 coin icin ayni BTC verisi 100 kez
    ayri ayri cekilirdi). Altcoin'lerin gercekten BTC'den bagimsiz mi guclu
    oldugu (RS) bunun uzerinden hesaplanir."""
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
    gore olceklemek icin kullanilir (sabit %X yerine, coin'in kendi
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
    """1d/5m EMA, MACD, RSI, BB% ve BTC'ye gore goreceli guc (RS) sinyallerini
    tek bir konfluens skoruna (-6..+6) birlestirir ve GUCLU_AL/AL/NOTR/SAT/
    GUCLU_SAT etiketine cevirir. Boylece 6 ayri sutuna tek tek bakip kafada
    birlestirmek yerine, tek sutunda net bir AL/SAT karari gorulur.

    RS (rel_strength = coin'in gunluk% - BTC'nin gunluk%): altcoin BTC ile
    birlikte mi surukleniyor yoksa BTC'den bagimsiz gercekten guclu mu
    oldugunu ayirt eder.

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

        info = tk.get_info()
        current_price = info.get("regularMarketPrice") or last_close
        prev_close = info.get("regularMarketPreviousClose") or (
            float(closes.iloc[-2]) if len(closes) >= 2 else last_close
        )

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

        # Goreceli guc (RS): coin'in gunluk % degisimi ile BTC'nin gunluk %
        # degisimi arasindaki fark. Kripto piyasasinda BTC "piyasanin
        # kendisi" gibi davranir - RS altcoin'in BTC'den bagimsiz mi guclu
        # oldugunu ayirt eder.
        benchmark_pct = get_benchmark_daily_pct()
        rel_strength = (
            daily_pct - benchmark_pct if (daily_pct == daily_pct and benchmark_pct is not None) else float("nan")
        )

        score_raw = confluence_score_raw(signal_1d, signal_5m, signal_macd, rsi_val, bb_percent, rel_strength)
        score = confluence_label(signal_1d, signal_5m, signal_macd, rsi_val, bb_percent, adx_val, rel_strength)

        # ATR bazli stop-loss/hedef: sinyalin yonune (AL/SAT) gore, coin'in
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
        day_high = info.get("regularMarketDayHigh")
        day_low = info.get("regularMarketDayLow")
        if day_high and day_low and day_high != day_low:
            day_range_pos = (current_price - day_low) / (day_high - day_low) * 100
        else:
            day_range_pos = float("nan")

        return {
            # NOT: ticker HAM Yahoo sembolu olarak saklanir (orn. "PEPE24478-USD"),
            # goruntu adi degil - fetch_with_retry'nin basarili/basarisiz takibi
            # bu alana gore yapiliyor, erken donusum eslesmeyi bozar. Temiz
            # gorunum adi sadece render() icinde display_symbol() ile uygulanir.
            "ticker": ticker,
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
    rows = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fetch_row, t): t for t in tickers}
        for future in as_completed(futures):
            row = future.result()
            if row:
                rows.append(row)
    return rows


def fetch_with_retry(tickers, max_workers: int = 12, max_retries: int = MAX_RETRIES):
    """Basarisiz (None donen) coin'leri birkac kez tekrar dener, boylece
    gecici agi/istek hatalari yuzunden coin eksik kalmaz."""
    rows_by_ticker = {}
    pending = list(dict.fromkeys(tickers))  # sirali + tekrarsiz

    for _ in range(max_retries + 1):
        if not pending:
            break
        for row in fetch_all(pending, max_workers=max_workers):
            rows_by_ticker[row["ticker"]] = row
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
    """Skoru GUCLU_AL/GUCLU_SAT'a DONEN (onceki dongude farkli olan) coin'leri
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
                    "ticker": display_symbol(ticker),
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


def log_score_history(rows):
    """log_signal_changes'in aksine SADECE GUCLU_AL/SAT'a donuste degil, HER
    dongude TUM coinler icin bir satir yazar. Web arayuzundeki Skor Gecmisi
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


def fmt_price(val: float) -> str:
    """Kripto fiyatlari BTC (onbinler) ile SHIB/PEPE (milyonda birkac dolar)
    arasinda cok genis bir araliga yayiliyor - sabit 2 ondalikli format
    kucuk fiyatli coin'leri hep "0.00" gosterirdi, bu yuzden fiyata gore
    ondalik basamak sayisi degisir."""
    if val != val:  # NaN check
        return "-"
    if val >= 1:
        return f"{val:,.2f}"
    if val >= 0.01:
        return f"{val:.4f}"
    return f"{val:.8f}"


def fmt_stop_target(stop, target) -> str:
    """Stop-loss/hedef ciftini 'stop/hedef' seklinde tek bir kompakt metne
    cevirir (fmt_price ile ayni adaptif ondalik mantigi). Yon belirsizse
    (NOTR sinyal) ikisi de None olur, '-' gosterilir."""
    if stop is None or target is None:
        return "-"
    return f"{fmt_price(stop)}/{fmt_price(target)}"


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
    print(f"En Buyuk {TOP_N} Kripto Para - Getiri Tablosu ({len(rows)} coin)   (guncelleme: {now})")
    print(f"Kaynak: Sabit liste, piyasa degerine gore (Yahoo'da kripto icin dinamik tarama yok)   |  Siralama: Once Skor gucu (GUCLU AL->AL->NOTR->SAT->GUCLU SAT), ayni grup icinde gunluk % (yuksekten dusuge)")
    print(f"Sinyal: EMA{EMA_FAST}/EMA{EMA_SLOW} (1d/5dk) + MACD({MACD_FAST},{MACD_SLOW},{MACD_SIGNAL}) + RSI({RSI_PERIOD}) + BB({BB_PERIOD},{BB_STD}sigma)   |  her {REFRESH_SECONDS} sn'de bir yenilenir")
    if missing_count:
        print(f"Uyari: {missing_count} coin icin veri alinamadi.")
    print()

    header = (
        f"{'#':>2} {'Skor':^4} {'1d':^2} {'5m':^2} {'MACD':^4} {'Coin':<8} {'Fiyat($)':>12} "
        f"{'Hacim':>5} {'GunPoz':>6} {'RSI':>3} {'BB%':>4} {'RS':>8} {'Gunluk':>8} "
        f"{'GunStop/Hedef':>22} {'SwStop/Hedef':>22}"
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
            f"{display_symbol(row['ticker']):<8} {fmt_price(row['price']):>12} "
            f"{fmt_ratio(row['volume_ratio']):>5} {fmt_range_pos(row['day_range_pos']):>6} "
            f"{fmt_rsi(row['rsi']):>3} {fmt_range_pos(row['bb_percent']):>4} "
            f"{fmt_pct(row['rel_strength']):>8} {fmt_pct(row['daily']):>8} "
            f"{fmt_stop_target(row['day_stop'], row['day_target']):>22} "
            f"{fmt_stop_target(row['swing_stop'], row['swing_target']):>22}"
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
    while True:
        rows_by_ticker, still_missing = fetch_with_retry(CRYPTO_TICKERS)
        top_rows = list(rows_by_ticker.values())
        missing_count = len(CRYPTO_TICKERS) - len(top_rows)

        if top_rows:
            render(top_rows, missing_count=missing_count)
            play_refresh_beep()
            log_signal_changes(top_rows)
            log_score_history(top_rows)
        else:
            print("Veri cekilemedi, tekrar denenecek...")
        try:
            time.sleep(REFRESH_SECONDS)
        except KeyboardInterrupt:
            sys.exit(0)


if __name__ == "__main__":
    main()
