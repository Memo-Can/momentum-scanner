#!/usr/bin/env python3
"""BIST genelinde gunun en cok kazandiran hisseleri gunluk yuzde degisime
gore siralayip listeler (toplam TOP_N=50 hisse). Hisse listesi iki kaynaktan
olusur:
1) BIST30 endeksindeki 30 hisse - GUNLUK % NE OLURSA OLSUN her zaman listede
   yer alir (buyuk/likit hisseler nadiren %3 esigini gectigi icin salt
   "en cok kazandiran" taramasinda kaybolabiliyorlardi, bu yuzden sabitlendi).
2) Yahoo Finance'in ozel taramasindan (percentchange > 3, region = tr)
   dinamik olarak cekilen, BIST30 disindaki en cok kazandiran ek hisseler -
   toplam goruntu sayisi TOP_N'e tamamlanana kadar (50-30=20 ek hisse).

Her REFRESH_SECONDS (60sn) saniyede bir hem tarayici listesi hem de getiriler
Yahoo Finance'ten yeniden cekilip terminal yenilenir.

Sutunlar:
  #        Gunluk getiriye gore siralamadaki yeri (1 = en cok kazandiran)
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
  Gunluk   Canli fiyat ile Yahoo'nun canli "onceki kapanis" alani arasindaki
           yuzde fark (satir rengi buna gore yesil/kirmizi olur, liste bu
           sutuna gore buyukten kucuge siralanir)
  Haftalik/Aylik  Zaten cekilmis gunluk kapanis serisinden (6 aylik) pandas
           resample ile turetilen hafta/ay acilisina gore canli fiyatin yuzde
           farki - ayrica Yahoo'dan haftalik/aylik mum cekilmez, her
           REFRESH_SECONDS'ta ek istek olmadan guncellenir.

Onbellek: Sadece 5dk sinyali INTRADAY_CACHE_SECONDS (5 dakika) boyunca
onbellekten dondurulur, boylece Yahoo'ya her REFRESH_SECONDS'ta gereksiz
istek atilmaz. Haftalik/aylik veri onbellek gerektirmez (ek istek atmaz).

Ses: Listede en az bir AL sinyali (5m) varsa al_beep.wav, en az bir SAT
varsa sat_beep.wav calinir (1d sinyali icin ses yok).
"""

import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import yfinance as yf

REFRESH_SECONDS = 60
TOP_N = 50
CANDIDATE_BUFFER = 30  # eksik hisse kalmamasi icin ihtiyactan fazla aday cekilir
MAX_RETRIES = 2
MIN_PRICE = 1  # kurusun altindaki cop kagitlari elemek icin fiyat tabani (TL)
INTRADAY_CACHE_SECONDS = 5 * 60  # 5dk mum verisi 5 dakikada bir yenilenir

SOUND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sounds")
AL_BEEP = os.path.join(SOUND_DIR, "al_beep.wav")
SAT_BEEP = os.path.join(SOUND_DIR, "sat_beep.wav")

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


def period_open_from_daily(hist, rule: str):
    """Gunluk mum verisinden (hist) mevcut haftanin/ayin acilis fiyatini pandas
    resample ile turetir. Boylece Yahoo'dan ayrica haftalik/aylik mum
    (interval=1wk/1mo) cekmeye gerek kalmaz - zaten cekilmis 6 aylik gunluk
    veri yeniden kullanilir, ek istek/onbellek gerektirmez ve her REFRESH_SECONDS'ta
    guncel kalir."""
    if hist.empty:
        return None
    opens = hist["Open"].resample(rule).first().dropna()
    if opens.empty:
        return None
    return float(opens.iloc[-1])


_intraday_signal_cache = {}  # ticker -> (fetched_at, signal)


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

        # Haftalik/aylik degisim, zaten cekilmis olan gunluk kapanis serisinden
        # (hist) resample ile turetilen donem acilisina gore hesaplanir - ekstra
        # Yahoo istegi gerekmez.
        weekly_open = period_open_from_daily(hist, "W")
        monthly_open = period_open_from_daily(hist, "MS")
        weekly_pct = pct_change(current_price, weekly_open)
        monthly_pct = pct_change(current_price, monthly_open)

        signal_5m = get_intraday_signal(tk, ticker)
        # Gunluk EMA kesisimi: zaten cekilmis olan gunluk kapanis serisi (closes)
        # uzerinden hesaplanir, ekstra Yahoo istegi gerekmez.
        signal_1d = ema_trend_signal(closes)

        # MACD/RSI/Bollinger: gunluk kapanis serisi (closes) uzerinden
        # hesaplanir, ekstra Yahoo istegi gerekmez.
        signal_macd = macd_signal(closes)
        rsi_val = rsi(closes)
        bb_percent = bollinger_percent_b(closes)

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
            "weekly": weekly_pct,
            "monthly": monthly_pct,
            "signal_5m": signal_5m,
            "signal_1d": signal_1d,
            "signal_macd": signal_macd,
            "rsi": rsi_val,
            "bb_percent": bb_percent,
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
    """Basarisiz (None donen) hisseleri birkac kez tekrar dener,
    boylece gecici agi/istek hatalari yuzunden hisse eksik kalmaz."""
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
RESET = "\033[0m"

SIGNAL_SYMBOLS = {"AL": "▲", "SAT": "▼", "NOTR": "–"}
SIGNAL_COLORS = {"AL": GREEN, "SAT": RED, "NOTR": YELLOW}


def play_beep(path: str):
    try:
        subprocess.Popen(["aplay", "-q", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def play_signal_sounds(rows):
    signals = {row["signal_5m"] for row in rows}
    if "AL" in signals:
        play_beep(AL_BEEP)
    if "SAT" in signals:
        play_beep(SAT_BEEP)


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


def sign_color(val: float, fallback: str) -> str:
    """Degerin kendi isaretine gore renk dondurur (NaN ise fallback/satir rengi)."""
    if val != val:  # NaN check
        return fallback
    return GREEN if val >= 0 else RED


def render(rows, missing_count: int = 0):
    use_color = sys.stdout.isatty()
    if use_color:
        os.system("cls" if os.name == "nt" else "clear")
    rows_sorted = sorted(rows, key=lambda r: r["daily"], reverse=True)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"BIST30 + En Cok Kazandiranlar - Getiri Tablosu ({len(rows)} hisse)   (guncelleme: {now})")
    print(f"Kaynak: BIST30 (daima dahil) + ozel tarama (fiyat/hacim filtresi YOK - DENEME)   |  Siralama: Gunluk yuzde degisime gore (yuksekten dusuge)")
    print(f"Sinyal: EMA{EMA_FAST}/EMA{EMA_SLOW} (1d/5dk) + MACD({MACD_FAST},{MACD_SLOW},{MACD_SIGNAL}) + RSI({RSI_PERIOD}) + BB({BB_PERIOD},{BB_STD}sigma)   |  her {REFRESH_SECONDS} sn'de bir yenilenir")
    if missing_count:
        print(f"Uyari: {missing_count} BIST30 hissesi icin veri alinamadi.")
    print()

    header = f"{'#':>3} {'1d':^3} {'5m':^3} {'MACD':^4} {'Hisse':<6} {'Sektor':<6} {'Fiyat':>7} {'Hacim':>5} {'GunPoz':>6} {'RSI':>4} {'BB%':>5} {'Gunluk':>8} {'Haftalik':>8} {'Aylik':>8}"
    print(header)
    print("-" * len(header))

    for i, row in enumerate(rows_sorted, start=1):
        symbol_1d = f"{SIGNAL_SYMBOLS.get(row['signal_1d'], row['signal_1d']):^3}"
        symbol_5m = f"{SIGNAL_SYMBOLS.get(row['signal_5m'], row['signal_5m']):^3}"
        symbol_macd = f"{SIGNAL_SYMBOLS.get(row['signal_macd'], row['signal_macd']):^4}"
        idx = f"{i:>3}"
        prefix = (
            f"{row['ticker']:<6} {row['sector']:<6} {row['price']:>7.2f} "
            f"{fmt_ratio(row['volume_ratio']):>5} {fmt_range_pos(row['day_range_pos']):>6} "
            f"{fmt_rsi(row['rsi']):>4} {fmt_range_pos(row['bb_percent']):>5} "
            f"{fmt_pct(row['daily']):>8} "
        )
        weekly_str = f"{fmt_pct(row['weekly']):>8}"
        monthly_str = f"{fmt_pct(row['monthly']):>8}"
        if use_color:
            row_color = GREEN if row["daily"] >= 0 else RED
            idx = f"{row_color}{idx}{RESET}"
            symbol_1d = f"{SIGNAL_COLORS.get(row['signal_1d'], YELLOW)}{symbol_1d}{RESET}"
            symbol_5m = f"{SIGNAL_COLORS.get(row['signal_5m'], YELLOW)}{symbol_5m}{RESET}"
            symbol_macd = f"{SIGNAL_COLORS.get(row['signal_macd'], YELLOW)}{symbol_macd}{RESET}"
            prefix = f"{row_color}{prefix}{RESET}"
            weekly_str = f"{sign_color(row['weekly'], row_color)}{weekly_str}{RESET}"
            monthly_str = f"{sign_color(row['monthly'], row_color)}{monthly_str}{RESET}"
        print(f"{idx} {symbol_1d} {symbol_5m} {symbol_macd} {prefix}{weekly_str} {monthly_str}")

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
            play_signal_sounds(top_rows)
        else:
            print("Veri cekilemedi, tekrar denenecek...")
        try:
            time.sleep(REFRESH_SECONDS)
        except KeyboardInterrupt:
            sys.exit(0)


if __name__ == "__main__":
    main()
