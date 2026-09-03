#!/usr/bin/env python3
"""ABD'de gunun en cok kazandiran hisseleri gunluk yuzde degisime gore
siralayip listeler (toplam TOP_N=50 hisse). Calisma mantigi scanner_bistTop50
ile birebir aynidir - hisse listesi iki kaynaktan olusur:
1) ABD'nin en buyuk 30 hissesi (sabit liste, TOP_US_TICKERS) - GUNLUK % NE
   OLURSA OLSUN her zaman listede yer alir (NVDA gibi buyuk/likit hisseler
   nadiren %3 esigini gectigi icin salt "en cok kazandiran" taramasinda
   kaybolabiliyorlardi, bu yuzden sabitlendi).
2) Yahoo Finance'in ozel taramasindan (percentchange > 3, region = US,
   fiyat >= MIN_PRICE, dayvolume > 15000) dinamik olarak cekilen, sabit
   listenin disindaki en cok kazandiran ek hisseler - toplam goruntu sayisi
   TOP_N'e tamamlanana kadar (50-30=20 ek hisse).

NOT: Yahoo'nun hazir "day_gainers" taramasi 5$ fiyat siniri kullanir ve bu
GPRO, SSM gibi 5$ altindaki gercek gainer'lari disarida birakir. Burada
MIN_PRICE = 1$ kullanilir: bu esik hem GPRO/SSM gibi hisseleri listeye
dahil eder hem de kurusun altinda fiyatlanan, hacmi/verisi guvenilir
olmayan OTC/warrant kagitlarini (ornegin tek islemle %1000+ gorunen
fiyatlari) disarida tutar. Fiyat siniri tamamen kaldirildiginda liste bu
tur cop kagitlarla doluyor ve GPRO/SSM yine de ust siralara giremiyor
(test edildi).

Sutunlar:
  #        Gunluk getiriye gore siralamadaki yeri (1 = en cok kazandiran)
  1d       Gunluk mumda EMA9/EMA21'e gore SUREKLI trend durumu (AL/SAT/NOTR)
  5m       5 dakikalik mumda EMA9/EMA21'e gore SUREKLI trend durumu
           (1d ile ayni mantik, farkli zaman dilimi; ikisi de kesisim aninda
           degil, EMA9'un EMA21'e gore o anki konumuna gore surekli AL/SAT
           gosterir). Renk: AL=yesil, SAT=kirmizi, NOTR=sari - satirin genel
           renginden (Gunluk +/-'ye gore) bagimsizdir.
  Sektor   Hissenin kisa sektor kodu. Hisse listesi dinamik oldugu icin sabit
           bir harita yerine Yahoo'nun canli "sector" alani (SECTOR_TRANSLATE
           ile kisa koda cevrilir) kullanilir; sektor degismedigi icin
           ticker basina suresiz onbelleklenir (_sector_cache).
  Fiyat    Canli fiyat (fast_info.lastPrice)
  Hacim    Bugunku islem hacminin 10 gunluk ortalama hacme orani (orn. 2.3x).
           Yuksek oran, fiyat hareketinin gercek katilimla desteklendigini gosterir.
  GunPoz   Fiyatin gunun dip-zirve araligindaki yeri (%). %100=gunun zirvesi
           (alici baskin), %0=gunun dibi (satici baskin).
  Gunluk   fast_info.lastPrice ile fast_info.previousClose arasindaki yuzde
           fark (satir rengi buna gore yesil/kirmizi olur, liste bu sutuna
           gore buyukten kucuge siralanir)
  Haftalik/Aylik  Yahoo'nun kendi haftalik/aylik mum verisindeki (1wk/1mo)
           donemin acilis fiyatina gore canli fiyatin yuzde farki

Onbellek: Haftalik/aylik veri 30 dakikada, 5dk sinyali 5 dakikada bir yenilenir
(WEEKLY_MONTHLY_CACHE_SECONDS / INTRADAY_CACHE_SECONDS) - her REFRESH_SECONDS'ta
degil, boylece Yahoo'ya gereksiz istek atilmaz.

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
MIN_PRICE = 1  # kurusun altindaki OTC/cop kagitlari elemek icin fiyat tabani ($)
WEEKLY_MONTHLY_CACHE_SECONDS = 30 * 60  # haftalik/aylik veri 30 dakikada bir yenilenir
INTRADAY_CACHE_SECONDS = 5 * 60  # 5dk mum verisi 5 dakikada bir yenilenir

SOUND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sounds")
AL_BEEP = os.path.join(SOUND_DIR, "al_beep.wav")
SAT_BEEP = os.path.join(SOUND_DIR, "sat_beep.wav")

# ABD'nin (yaklasik) piyasa degerine gore en buyuk 30 hissesi - gunluk % ne
# olursa olsun listede daima yer alirlar (bkz. dosya basindaki aciklama).
TOP_US_TICKERS = [
    "NVDA", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "AVGO", "TSLA", "BRK-B",
    "LLY", "WMT", "JPM", "V", "ORCL", "MA", "NFLX", "XOM", "COST", "PG",
    "JNJ", "HD", "ABBV", "BAC", "CVX", "KO", "AMD", "PLTR", "GE", "CSCO",
    "TMUS",
]


EMA_FAST = 9
EMA_SLOW = 21

# Yahoo'nun standart (Ingilizce) sektor isimlerini kisa Turkce kodlara cevirir.
# Hisse listesi dinamik oldugu icin (gunun kazandiranlari surekli degisir),
# sabit bir ticker->sektor haritasi yerine Yahoo'nun her hisse icin dondurdugu
# genel "sector" alani kullanilir.
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


def latest_period_open(hist):
    """Yahoo'nun kendi haftalik/aylik mumundaki en son GECERLI (NaN olmayan)
    donemin acilis fiyatini dondurur. Icinde bulunulan hafta/ay tatille
    basladiysa (ornegin ayin 1'i resmi tatilse) o satir NaN gelebiliyor,
    bu durumda bir onceki tamamlanmis donemin acilisi kullanilir."""
    if hist.empty:
        return None
    valid = hist.dropna(subset=["Open"])
    if valid.empty:
        return None
    return float(valid["Open"].iloc[-1])


_weekly_monthly_cache = {}  # ticker -> (fetched_at, weekly_open, monthly_open)
_intraday_signal_cache = {}  # ticker -> (fetched_at, signal)
_sector_cache = {}  # ticker -> sektor kodu (sektor degismedigi icin suresiz onbelleklenir)


def get_sector(tk, ticker: str) -> str:
    """Hissenin sektorunu suresiz onbellekten dondurur; sektor siniflandirmasi
    gun icinde degismedigi icin tekrar tekrar cekmenin anlami yok."""
    cached = _sector_cache.get(ticker)
    if cached is not None:
        return cached
    try:
        sector_en = tk.get_info().get("sector")
    except Exception:
        sector_en = None
    sector = SECTOR_TRANSLATE.get(sector_en, sector_en[:4].upper() if sector_en else "-")
    _sector_cache[ticker] = sector
    return sector


def get_weekly_monthly_open(tk, ticker: str):
    """Haftalik/aylik acilis fiyatlarini WEEKLY_MONTHLY_CACHE_SECONDS boyunca
    onbellekten dondurur; Yahoo'ya her REFRESH_SECONDS'ta bir degil, 30 dakikada
    bir istek atilir (bu veri zaten dakikalar icinde degismiyor)."""
    cached = _weekly_monthly_cache.get(ticker)
    if cached and time.time() - cached[0] < WEEKLY_MONTHLY_CACHE_SECONDS:
        return cached[1], cached[2]
    weekly_hist = tk.history(period="3mo", interval="1wk", auto_adjust=False)
    weekly_open = latest_period_open(weekly_hist)
    monthly_hist = tk.history(period="8mo", interval="1mo", auto_adjust=False)
    monthly_open = latest_period_open(monthly_hist)
    _weekly_monthly_cache[ticker] = (time.time(), weekly_open, monthly_open)
    return weekly_open, monthly_open


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


def build_gainers_query():
    # Yahoo'nun "day_gainers" taramasiyla ayni kriterler, ancak 5$ yerine
    # MIN_PRICE (1$) fiyat tabani kullanilir -> GPRO/SSM gibi hisseler
    # listeye girebilirken kurusun altindaki cop kagitlar elenir.
    return yf.EquityQuery(
        "and",
        [
            yf.EquityQuery("gt", ["percentchange", 3]),
            yf.EquityQuery("eq", ["region", "us"]),
            yf.EquityQuery("gt", ["dayvolume", 15000]),
            yf.EquityQuery("gte", ["intradayprice", MIN_PRICE]),
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
        hist = tk.history(period="2mo", interval="1d", auto_adjust=False)
        if hist.empty:
            return None

        closes = hist["Close"]
        last_close = float(closes.iloc[-1])

        fast = tk.fast_info
        current_price = fast.get("lastPrice") or last_close
        prev_close = fast.get("previousClose") or (
            float(closes.iloc[-2]) if len(closes) >= 2 else last_close
        )

        # Gunluk degisim: canli fiyat vs Yahoo'nun canli "onceki kapanis" alani.
        # (closes.iloc[-2] gibi gecmis mum dizisinden indeksle secim yapmak,
        # Yahoo'nun gunluk mum verisinde bir gun eksik geldigi durumlarda yanlis
        # referans gunu secip degisimi ciddi sekilde yanlis hesaplayabiliyordu -
        # ornegin TKNKA'da 2 Eylul verisi eksikti, gercekte %8.4 olan gunluk
        # degisim bu yuzden %19.3 gorunmustu.)
        daily_pct = pct_change(current_price, prev_close)

        # Haftalik/aylik degisim, Yahoo'nun kendi haftalik/aylik mum verisindeki
        # (interval="1wk"/"1mo") donemin acilis fiyatina gore hesaplanir.
        weekly_open, monthly_open = get_weekly_monthly_open(tk, ticker)
        weekly_pct = pct_change(current_price, weekly_open)
        monthly_pct = pct_change(current_price, monthly_open)

        signal_5m = get_intraday_signal(tk, ticker)
        # Gunluk EMA kesisimi: zaten cekilmis olan gunluk kapanis serisi (closes)
        # uzerinden hesaplanir, ekstra Yahoo istegi gerekmez.
        signal_1d = ema_trend_signal(closes)

        # Hacim orani: bugunku hacmin 10 gunluk ortalama hacme orani.
        # Yuksek oran (>1.5x gibi) sinyalin gercek katilimla desteklendigini gosterir.
        volume = fast.get("lastVolume")
        avg_volume = fast.get("tenDayAverageVolume")
        volume_ratio = (volume / avg_volume) if (volume and avg_volume) else float("nan")

        # Gun ici pozisyon: fiyatin gunun dip-zirve araligindaki yeri (%).
        # %100'e yakin = gun icinde alis baskisi guclu, %0'a yakin = satis baskisi guclu.
        day_high = fast.get("dayHigh")
        day_low = fast.get("dayLow")
        if day_high and day_low and day_high != day_low:
            day_range_pos = (current_price - day_low) / (day_high - day_low) * 100
        else:
            day_range_pos = float("nan")

        sector = get_sector(tk, ticker)

        return {
            "ticker": ticker,
            "sector": sector,
            "price": current_price,
            "daily": daily_pct,
            "weekly": weekly_pct,
            "monthly": monthly_pct,
            "signal_5m": signal_5m,
            "signal_1d": signal_1d,
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
    print(f"ABD Buyuk 30 + En Cok Kazandiranlar - Getiri Tablosu ({len(rows)} hisse)   (guncelleme: {now})")
    print(f"Kaynak: ABD'nin en buyuk 30 hissesi (daima dahil) + ozel tarama (fiyat>={MIN_PRICE}$, hacim>15000)   |  Siralama: Gunluk yuzde degisime gore (yuksekten dusuge)")
    print(f"Sinyal: EMA{EMA_FAST}/EMA{EMA_SLOW} kesisimi (5dk mum)   |  her {REFRESH_SECONDS} sn'de bir yenilenir")
    if missing_count:
        print(f"Uyari: {missing_count} buyuk ABD hissesi icin veri alinamadi.")
    print()

    header = f"{'#':>3} {'1d':^3} {'5m':^3} {'Hisse':<6} {'Sektor':<6} {'Fiyat':>9} {'Hacim':>5} {'GunPoz':>6} {'Gunluk':>8} {'Haftalik':>8} {'Aylik':>8}"
    print(header)
    print("-" * len(header))

    for i, row in enumerate(rows_sorted, start=1):
        symbol_1d = f"{SIGNAL_SYMBOLS.get(row['signal_1d'], row['signal_1d']):^3}"
        symbol_5m = f"{SIGNAL_SYMBOLS.get(row['signal_5m'], row['signal_5m']):^3}"
        idx = f"{i:>3}"
        prefix = (
            f"{row['ticker']:<6} {row['sector']:<6} {row['price']:>9.2f} "
            f"{fmt_ratio(row['volume_ratio']):>5} {fmt_range_pos(row['day_range_pos']):>6} "
            f"{fmt_pct(row['daily']):>8} "
        )
        weekly_str = f"{fmt_pct(row['weekly']):>8}"
        monthly_str = f"{fmt_pct(row['monthly']):>8}"
        if use_color:
            row_color = GREEN if row["daily"] >= 0 else RED
            idx = f"{row_color}{idx}{RESET}"
            symbol_1d = f"{SIGNAL_COLORS.get(row['signal_1d'], YELLOW)}{symbol_1d}{RESET}"
            symbol_5m = f"{SIGNAL_COLORS.get(row['signal_5m'], YELLOW)}{symbol_5m}{RESET}"
            prefix = f"{row_color}{prefix}{RESET}"
            weekly_str = f"{sign_color(row['weekly'], row_color)}{weekly_str}{RESET}"
            monthly_str = f"{sign_color(row['monthly'], row_color)}{monthly_str}{RESET}"
        print(f"{idx} {symbol_1d} {symbol_5m} {prefix}{weekly_str} {monthly_str}")

    print(f"\nCikmak icin CTRL+C")
    sys.stdout.flush()


def main():
    pinned_symbols = set(TOP_US_TICKERS)
    while True:
        gainer_tickers = fetch_top_gainer_tickers(TOP_N + CANDIDATE_BUFFER)
        # ABD'nin en buyuk 30 hissesi her zaman aday havuzunda - gunluk % esigini
        # gecmese bile (ornegin NVDA gibi buyuk/likit bir hisse) listeden hic
        # dislanmasin diye.
        candidate_tickers = list(dict.fromkeys(TOP_US_TICKERS + gainer_tickers))
        rows_by_ticker, still_missing = fetch_with_retry(candidate_tickers) if candidate_tickers else ({}, [])

        pinned_rows = [r for r in rows_by_ticker.values() if r["ticker"] in pinned_symbols]
        other_rows = [r for r in rows_by_ticker.values() if r["ticker"] not in pinned_symbols]
        other_rows_sorted = sorted(other_rows, key=lambda r: r["daily"], reverse=True)

        # Sabit 30 hisse her zaman dahil; kalan yerler en cok kazandiran diger hisselerle doldurulur.
        extra_slots = max(0, TOP_N - len(pinned_rows))
        top_rows = pinned_rows + other_rows_sorted[:extra_slots]
        missing_count = max(0, len(TOP_US_TICKERS) - len(pinned_rows))

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
