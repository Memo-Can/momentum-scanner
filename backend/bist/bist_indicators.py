"""Teknik analiz indikatorleri + Konfluens Skoru hesaplama - SAF matematik
(bkz. CLAUDE.md "Başka hangi dosyalar oluşturulabilir" analizi). Dogrudan
calistirilmaz.

`notification.py` ile AYNI mimari ilke: bu dosya projedeki HICBIR modulu
(bist_scanner.py, bist_web.py vb.) import ETMEZ - sadece stdlib + pandas
kullanan tam bagimsiz bir "yaprak" (leaf) modul. Tum fonksiyonlar PUR: aynı
girdiyle HER ZAMAN ayni cikti, hicbir DB/Yahoo/ag erisimi veya global state
mutasyonu YOK - sadece pandas Series/float uzerinde matematik. Bu yuzden
bist_scanner.py (ve bist_web.py/bist_terminal.py/bist_backfill_daily.py)
bu dosyayi GUVENLE import edebilir, DAIRESEL IMPORT riski hic yoktur.

NEDEN BENCHMARK_TICKER/ATR_DAY_MULT/ATR_SWING_MULT/ATR_TARGET_R/
WEEKLY_LOOKBACK_DAYS/MONTHLY_LOOKBACK_DAYS/YEARLY_LOOKBACK_DAYS BURAYA
TASINMADI, hala bist_scanner.py'de: bunlar "hangi ATR katsayisini/hangi
Yahoo sembolunu/hangi pencereyi KULLANIYORUZ" turunde STRATEJI/konfigurasyon
sabitleri - indikatorun KENDI matematigine (ATR_PERIOD gibi) ait degil,
stop_target_levels()'a DISARIDAN parametre olarak geciliyor. Ayrim: bu
dosya "bir indikator degeri/skor NASIL hesaplanir", bist_scanner.py "hangi
parametrelerle/hangi veriyle cagiriyoruz" sorusuna cevap verir.

SCORE_ORDER da buraya tasindi (confluence_label()'in urettigi 7 etiketin
sira/agirlik sozlugu) - confluence_label ile AYNI "skor sozlugu" kavraminin
parcasi, DB/Yahoo'dan bagimsiz. _STRONG_BUY_ZONE/_STRONG_SIGNAL_LABELS ise
(hangi etiket alt kumesinin "guclu alim/sinyal" SAYILACAGI, bir strateji
karari) BURAYA TASINMADI, bist_scanner.py'de kaldi."""

import pandas as pd

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
RS_THRESHOLD = 0.5  # bu esigin altindaki fark (yuzde puan) NOTR sayilir
ATR_PERIOD = 14

# Siralama onceligi (bist_web.py/bist_terminal.py'nin sort_key'lerinde
# kullanilir): en guclu AL en ustte, en guclu SAT en altta - ayni skor
# grubunun icinde gunluk % buyukten kucuge sıralanir. GUCLU_SAT artik canli
# taramada hic uretilmiyor (bkz. confluence_label) ama gecmis DB
# kayitlarinda/defansif olarak burada duruyor.
SCORE_ORDER = {"MUTLAK_AL": 0, "GUCLU_AL": 1, "AL": 2, "NOTR": 3, "SAT": 4, "GUCLU_SAT": 5, "MUTLAK_SAT": 6}


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
    signal_macd: str,
    rsi_val: float,
    bb_percent: float,
    rel_strength: float,
) -> int:
    """confluence_label'in ADX filtresi uygulanmadan onceki ham -5..+5
    toplami. ADX aninlik bir guven filtresi oldugu icin (ortalamasi anlamli
    degil), Skor Gecmisi sekmelerinde gunluk/saatlik ortalama almak icin bu
    ham deger ayrica saklanir - bkz. bist_scanner.py: log_score_history.

    5 bilesen (2026-10-31'den once 6'ydi, signal_5m cikarildi - bkz.
    CLAUDE.md "5 dakikalik bilesen kaldirildi"): signal_1d, signal_macd,
    RSI, BB%, RS."""

    def cat(sig: str) -> int:
        return {"AL": 1, "SAT": -1}.get(sig, 0)

    score = cat(signal_1d) + cat(signal_macd)

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
    signal_macd: str,
    rsi_val: float,
    bb_percent: float,
    adx_val: float,
    rel_strength: float,
) -> str:
    """1d EMA, MACD, RSI, BB% ve endekse gore goreceli guc (RS) sinyallerini
    tek bir konfluens skoruna (-5..+5) birlestirir ve MUTLAK_AL/GUCLU_AL/AL/
    NOTR/SAT/GUCLU_SAT/MUTLAK_SAT etiketine cevirir. Boylece 5 ayri sutuna
    tek tek bakip kafada birlestirmek yerine, tek sutunda net bir AL/SAT
    karari gorulur.

    RS (rel_strength = hissenin gunluk% - endeksin gunluk%): hisse piyasayla
    birlikte mi surukleniyor yoksa piyasadan bagimsiz gercekten guclu mu
    oldugunu ayirt eder - endeksin +%4 oldugu bir gunde hissenin +%5 olmasi,
    endeksin duz oldugu bir gunde +%5 olmasindan cok daha az anlamlidir.

    ADX, ayri bir sutun olarak degil, MUTLAK_AL/GUCLU_AL/GUCLU_SAT/MUTLAK_SAT
    etiketlerinin guvenilirligini teyit eden bir filtre olarak kullanilir:
    ADX<20 (zayif/yatay piyasa) veya hesaplanamiyorsa, bu etiketler normal
    AL/SAT'a dusurulur - boyle bir ortamda guclu/mutlak sinyale guvenmek
    yaniltici olabilir."""

    score = confluence_score_raw(signal_1d, signal_macd, rsi_val, bb_percent, rel_strength)

    # Esikler 2026-10-31'de (signal_5m kaldirilinca, maks |skor| 6'dan 5'e
    # dusunce) yeniden olceklendi - GUCLU_AL/SAT icin "en fazla 1 bilesen
    # cekimser, SIFIR karsit oy" kurali AYNEN korunuyor. 2026-11-01'de
    # kullanici istegiyle MUTLAK_AL/MUTLAK_SAT eklendi (bkz. CLAUDE.md
    # "MUTLAK AL/SAT etiketleri"): MUTLAK_AL SADECE tam mutabakat (5 bilesen
    # 5 AL, sifir cekimser/karsit) gerektirir - GUCLU_AL artik SADECE
    # skor==4'u (4 AL + 1 cekimser) kapsar, skor==5 MUTLAK_AL'a gider.
    # MUTLAK_SAT ise BILINCLI olarak daha GEVSEK bir esikle (skor<=-4, yani
    # eski GUCLU_SAT'in TUM araligi) tanimlandi - kullanicinin kendi secimi,
    # AL tarafiyla simetrik DEGIL. Bu yuzden GUCLU_SAT etiketi artik CANLI
    # taramada HIC URETILMEZ (sadece eski/gecmis DB kayitlarinda gorulebilir,
    # geriye donuk uyumluluk icin kod/frontend'den SILINMEDI).
    if score == 5:
        label = "MUTLAK_AL"
    elif score >= 4:
        label = "GUCLU_AL"
    elif score >= 2:
        label = "AL"
    elif score >= -1:
        label = "NOTR"
    elif score >= -3:
        label = "SAT"
    else:
        label = "MUTLAK_SAT"

    trend_confirmed = adx_val == adx_val and adx_val >= ADX_TREND_THRESHOLD
    if label in ("MUTLAK_AL", "GUCLU_AL") and not trend_confirmed:
        label = "AL"
    elif label in ("MUTLAK_SAT", "GUCLU_SAT") and not trend_confirmed:
        label = "SAT"

    return label
