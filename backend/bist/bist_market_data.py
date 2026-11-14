"""Yahoo Finance'ten veri cekme katmani - "nereden/nasil cekiyoruz" (bkz.
CLAUDE.md "Basta hangi dosyalar olusturulabilir" analizi, bist_indicators.py/
notification.py'nin ardindan). Dogrudan calistirilmaz.

`settings.py`/`admin.py` ile AYNI mimari ilke: bu dosya `bist_scanner.py`'yi
TEK YONLU import eder (SADECE `BIST_REFRESH_SECONDS` icin, get_benchmark_
daily_pct()'in onbellek suresi) - bist_scanner.py bu dosyayi HIC import
ETMEZ (bist_scanner.py'de kalan hicbir fonksiyon burdaki bir fonksiyonu
cagirmiyor - DOGRULANDI). Ayrica `bist_indicators.py`'yi de import eder (saf
matematik icin). Bu yuzden DAIRESEL IMPORT riski yoktur: bist_market_data.py ->
bist_scanner.py -> (bist_indicators.py, notification.py), bist_market_data.py ->
bist_indicators.py.

Icerigi: BIST evreninin tanimi (BIST30_TICKERS/BIST100_TICKERS), Yahoo'nun
region=tr taramasi (fetch_all_bist_tickers/fetch_all_bist_quotes/
fetch_top_gainers), hisse basina indikator+skor hesaplayan fetch_row() ve
onun coklu-thread/retry sarmalayicilari (fetch_all/fetch_with_retry), ust
seritteki doviz/emtia/BIST100/BTC gostergeleri (fetch_market_indicators)."""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import yfinance as yf

import bist_indicators as indicators
import bist_scanner as scanner

MAX_RETRIES = 2
TOP300_SIZE = 300  # BIST 300 sekmesi: gunluk % degisime gore ilk N (dinamik, endeks degil)
SCREEN_PAGE_SIZE = 250  # Yahoo'nun yf.screen() tek istekte donduregi maksimum sonuc sayisi
RETRY_BACKOFF_SECONDS = 3.0  # her retry denemesi ONCESI bekleme suresi (bkz. fetch_with_retry)
GRAMS_PER_TROY_OUNCE = 31.1034768

BENCHMARK_TICKER = "XU100.IS"  # BIST100 endeksi - goreceli guc (RS) karsilastirma bazı
ATR_DAY_MULT = 1.5  # gunluk (day trade) stop mesafesi = ATR * bu katsayi (dar)
ATR_SWING_MULT = 3.0  # swing stop mesafesi = ATR * bu katsayi (genis)
ATR_TARGET_R = 3.0  # hedef mesafesi = stop mesafesi * bu katsayi (risk:odul ~1:3 - 2026-10-30'da
# 1 yillik backfill verisiyle yapilan backtest sonucuna gore 2.0'dan yukseltildi: ayni stop
# genisliginde RR arttikca ortalama getiri tutarli sekilde artiyordu, bkz. CLAUDE.md "Al/satis
# seviyeleri (ATR_TARGET_R) - backtest").
WEEKLY_LOOKBACK_DAYS = 5  # "5D" - kayan pencere, takvim haftasi degil
MONTHLY_LOOKBACK_DAYS = 21  # "1M" - ~1 aylik islem gunu sayisi, kayan pencere
YEARLY_LOOKBACK_DAYS = 250  # "1Y" - ~1 yillik islem gunu sayisi (252'nin biraz altinda, tatil/kapanis farklarina karsi pay), kayan pencere

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

# Resmi BIST 30 Endeksi'nin (XU030) sabit uye listesi - 2026-11-12'de Midas'in
# (getmidas.com/canli-borsa/xu030-bist-30-hisseleri) kendi sayfasi WebFetch ile
# cekilip tekrar dogrulandi (onceki dogrulama 2026-09-01'di) - TEK fark DSTKF
# cikip TRMET girmis oldu, diger 29 hisse aynen kaldi.
# BIST30, BIST100_TICKERS'in bir ALT KUMESIDIR (dogrulandi) - bu yuzden
# BIST100 taramasi otomatik olarak BIST30'u da kapsar, ayri bir istek
# gerekmez.
BIST30_TICKERS = [
    "AEFES", "AKBNK", "ASELS", "ASTOR", "BIMAS", "EKGYO", "ENKAI", "EREGL",
    "FROTO", "GARAN", "GUBRF", "ISCTR", "KCHOL", "KRDMD", "MGROS", "PETKM",
    "PGSUS", "SAHOL", "SASA", "SISE", "TAVHL", "TCELL", "THYAO", "TOASO",
    "TRALT", "TRMET", "TTKOM", "TUPRS", "VAKBN", "YKBNK",
]
BIST30_TICKERS = [f"{t}.IS" for t in BIST30_TICKERS]

# Resmi BIST 100 Endeksi'nin (XU100) sabit uye listesi - 2026-11-12'de Midas'in
# (getmidas.com/canli-borsa/xu100-bist-100-hisseleri) kendi sayfasi WebFetch ile
# cekilip tekrar dogrulandi (onceki dogrulama 2026-09-01'di) - BEKLENENDEN BUYUK
# bir degisim cikti: 27 hisse cikti (BALSU/BSOKE/DAPGM/DSTKF/EFOR/ESEN/EUPWR/
# GENIL/GESAN/GRTHO/GSRAY/IEYHO/IZENR/KLRHO/KTLEV/KUYAS/MAGEN/MIATK/ODINE/
# PASEU/PATEK/PSGYO/QUAGR/RALYH/REEDR/SARKY/SKBNK), 27 yeni hisse girdi
# (AGHOL/AHGAZ/AKCNS/AKFYE/ALBRK/ANHYT/AYGAZ/BINHO/ECZYT/EGEEN/EGGUB/ENTRA/
# GLYHO/GWIND/ISDMR/KARSN/KATMR/KCAER/KORDS/LMKDC/RGYAS/RYSAS/SNGYO/TABGD/
# TCKRC/TRGYO/TTRAK) - normal ceyreklik rebalans'tan (genelde birkac hisse)
# COK daha buyuk bir oynama, muhtemelen bir endeks kurali/agirlik degisikligi.
# endeks ceyreklik yeniden dengelendigi icin zamanla guncellenmesi gerekebilir.
BIST100_TICKERS = [
    "AEFES", "AGHOL", "AHGAZ", "AKBNK", "AKCNS", "AKFYE", "AKSA", "AKSEN",
    "ALARK", "ALBRK", "ALTNY", "ANHYT", "ANSGR", "ARCLK", "ASELS", "ASTOR",
    "AYGAZ", "BERA", "BIMAS", "BINHO", "BRSAN", "BRYAT", "BTCIM", "CANTE",
    "CCOLA", "CIMSA", "CVKMD", "CWENE", "DOAS", "DOHOL", "ECILC", "ECZYT",
    "EGEEN", "EGGUB", "EKGYO", "ENERY", "ENJSA", "ENKAI", "ENTRA", "EREGL",
    "EUREN", "FENER", "FROTO", "GARAN", "GLRMK", "GLYHO", "GRSEL", "GUBRF",
    "GWIND", "HALKB", "HEKTS", "ISCTR", "ISDMR", "ISMEN", "KARSN", "KATMR",
    "KCAER", "KCHOL", "KORDS", "KRDMD", "LMKDC", "MAVI", "MGROS", "MPARK",
    "OBAMS", "ODAS", "OTKAR", "OYAKC", "PAHOL", "PETKM", "PGSUS", "RGYAS",
    "RYSAS", "SAHOL", "SASA", "SISE", "SNGYO", "SOKM", "TABGD", "TAVHL",
    "TCELL", "TCKRC", "THYAO", "TKFEN", "TOASO", "TRALT", "TRENJ", "TRGYO",
    "TRMET", "TSKB", "TTKOM", "TTRAK", "TUKAS", "TUPRS", "TURSG", "ULKER",
    "VAKBN", "VESTL", "YKBNK", "ZOREN",
]
BIST100_TICKERS = [f"{t}.IS" for t in BIST100_TICKERS]

# Yukaridaki iki listenin SON insan dogrulamasinin yapildigi tarih - endeks
# ceyreklik yeniden dengelendigi icin bu tarih eskiyince listelerin Borsa
# Istanbul'un resmi XU030/XU100 uyelik listesiyle tekrar karsilastirilmasi
# gerekir. Otomatik bir kaynaktan cekilmiyor (resmi API/scrape YOK, bilerek
# elle girilmis - bkz. yukaridaki yorumlar), bu yuzden otomatik
# GUNCELLENEMEZ; asagidaki kontrol SADECE servis her baslatildiginda bir
# konsol/journalctl hatirlatmasi basar.
BIST_INDEX_LISTS_VERIFIED_ON = "2026-11-12"


def _check_index_lists_freshness() -> None:
    verified = datetime.strptime(BIST_INDEX_LISTS_VERIFIED_ON, "%Y-%m-%d")
    days_since = (datetime.now() - verified).days
    if days_since > 95:  # ~1 ceyrek (endeks rebalans periyodu)
        print(
            f"[UYARI] BIST30/100_TICKERS listeleri {days_since} gundur dogrulanmadi "
            f"(son dogrulama: {BIST_INDEX_LISTS_VERIFIED_ON}) - Borsa Istanbul'un "
            f"resmi XU030/XU100 uye listesiyle karsilastir, gerekirse listeleri "
            f"guncelle ve BIST_INDEX_LISTS_VERIFIED_ON'u bugune cek."
        )


_check_index_lists_freshness()


_benchmark_cache = {}  # "pct" -> (fetched_at, daily_pct)


def get_benchmark_daily_pct():
    """BIST100 endeksinin gunluk yuzde degisimini BIST_REFRESH_SECONDS'a yakin
    bir sure onbellekten dondurur - her hisse icin ayri ayri degil, dongu basina
    bir kez cekilir (onbellek olmadan, 100+ hisse icin ayni endeks verisi
    100+ kez ayri ayri cekilirdi). Hisselerin gercekten piyasayla birlikte mi
    surukendigi yoksa piyasadan bagimsiz mi guclu oldugu (RS) bunun uzerinden
    hesaplanir."""
    cached = _benchmark_cache.get("pct")
    if cached and time.time() - cached[0] < scanner.BIST_REFRESH_SECONDS - 5:
        return cached[1]
    pct = None
    try:
        tk = yf.Ticker(BENCHMARK_TICKER)
        info = tk.get_info()
        pct = info.get("regularMarketChangePercent")
        if pct is None:
            hist = tk.history(period="5d", interval="1d", auto_adjust=False)
            if len(hist) >= 2:
                pct = indicators.pct_change(float(hist["Close"].iloc[-1]), float(hist["Close"].iloc[-2]))
    except Exception:
        pct = None
    _benchmark_cache["pct"] = (time.time(), pct)
    return pct


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
            pct = indicators.pct_change(price, prev) if prev else None
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
        return gram_try, indicators.pct_change(gram_try, prev_gram)

    gold_gram_price, gold_gram_pct = _derived_gram(gold_oz_price, gold_oz_pct)
    silver_gram_price, silver_gram_pct = _derived_gram(silver_oz_price, silver_oz_pct)

    return [
        {"label": "BIST100", "price": xu100_price, "pct": xu100_pct, "unit": ""},
        {"label": "Dolar", "price": usd_price, "pct": usd_pct, "unit": "TL"},
        {"label": "Euro", "price": eur_price, "pct": eur_pct, "unit": "TL"},
        {"label": "Sterlin", "price": gbp_price, "pct": gbp_pct, "unit": "TL"},
        {"label": "Bitcoin", "price": btc_price, "pct": btc_pct, "unit": "$"},
        {"label": "Gram Altin", "price": gold_gram_price, "pct": gold_gram_pct, "unit": "TL"},
        {"label": "Gram Gumus", "price": silver_gram_price, "pct": silver_gram_pct, "unit": "TL"},
        {"label": "Petrol", "price": oil_price, "pct": oil_pct, "unit": "$"},
    ]


def build_bist_query():
    """Yahoo'nun BIST (Borsa Istanbul) icin TUM piyasayi tarayan sorgusu -
    region="tr" butun BIST hisselerini kapsar, endeks uyeligi ayrimi/esik
    yoktur."""
    return yf.EquityQuery("eq", ["region", "tr"])


def fetch_all_bist_tickers():
    """Yahoo'nun region=tr taramasindaki TUM hisseleri sayfalama ile (tek
    istek max SCREEN_PAGE_SIZE donduruyor) toplar - hem canli tarama
    (bist_terminal.py/bist_web.py) hem de bist_backfill_daily.py
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


def fetch_all_bist_quotes():
    """fetch_all_bist_tickers() ile AYNI yf.screen() sayfalamasini kullanir
    ama SADECE sembolu DEGIL, temel fiyat/gunluk % bilgisini de tasir -
    628 hisse icin TOPLAM ~3 istek (hisse basina 1 DEGIL). fetch_row()'un
    pahali per-hisse indikator hesaplamasi (EMA/RSI/MACD/ADX/Confluence
    Score icin 1y gunluk OHLC gerektiriyor) burada YOK - bu yuzden skor/
    indikator sutunlari bu veriyle DOLDURULAMAZ, sadece ticker/fiyat/gunluk%
    icin kullanilir. bist_web.py: _scan_once() bunu, birlesik havuzun
    (BIST100+ilk 300 kazandiran) DISINDA kalan hisseleri de tabloda (skor
    sutunu bos/"BEKLIYOR" olarak) GORUNUR kilmak icin cagirir - 2026-11-03'te
    kullanici istegiyle eklendi: "tum hisseler gorunsun, skoru eksik
    olanlarda islem yapilmasin" dedi."""
    query = build_bist_query()
    quotes_out = []
    offset = 0
    try:
        while True:
            result = yf.screen(query, sortField="percentchange", sortAsc=False, size=SCREEN_PAGE_SIZE, offset=offset)
            quotes = result.get("quotes", [])
            if not quotes:
                break
            for q in quotes:
                symbol = q.get("symbol")
                if not symbol:
                    continue
                quotes_out.append({
                    "ticker": symbol.removesuffix(".IS"),
                    "price": q.get("regularMarketPrice"),
                    "daily": q.get("regularMarketChangePercent"),
                })
            offset += len(quotes)
            if offset >= result.get("total", 0):
                break
    except Exception:
        pass
    return quotes_out


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
        # mum gerekir - 2 ay yetersiz kalabildigi icin 6 aya, sonra 1Y
        # sutunu (YEARLY_LOOKBACK_DAYS) icin 1 yila cikarildi (2026-11-02) -
        # Yahoo istegi sayisi DEGISMEDI (hala hisse basina 1 istek), sadece
        # donen veri daha genis bir tarih araligini kapsiyor; bu ayrica
        # EMA/MACD/RSI/ADX gibi indikatorlerin yakinsamasini da iyilestiriyor
        # (daha uzun isinma payi).
        hist = tk.history(period="1y", interval="1d", auto_adjust=False)
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
            daily_pct = indicators.pct_change(current_price, prev_close)

        # Haftalik/aylik degisim, Yahoo'nun kendi haftalik/aylik mum verisindeki
        # (interval=1wk/1mo) donemin acilis fiyatina gore hesaplanir. Terminal
        # tablosunda gosterilmiyor, sadece web arayuzu icin.
        d5_pct = indicators.rolling_pct_change(closes, current_price, WEEKLY_LOOKBACK_DAYS)
        m1_pct = indicators.rolling_pct_change(closes, current_price, MONTHLY_LOOKBACK_DAYS)
        y1_pct = indicators.rolling_pct_change(closes, current_price, YEARLY_LOOKBACK_DAYS)

        # Gunluk EMA kesisimi: zaten cekilmis olan gunluk kapanis serisi (closes)
        # uzerinden hesaplanir, ekstra Yahoo istegi gerekmez. (2026-10-31'de
        # buradaki 5 dakikalik EMA bileseni - ayri bir Yahoo istegi gerektirdigi
        # ICIN - kaldirildi, bkz. CLAUDE.md "5 dakikalik bilesen kaldirildi":
        # hisse basina istek sayisini 3'ten 2'ye dusurup rate-limit riskini
        # azaltiyor, ayrica zaten gunluk mum canli fiyatla surekli guncellendigi
        # icin bilgi kaybi minimal.)
        signal_1d = indicators.ema_trend_signal(closes)

        # MACD/RSI/Bollinger: gunluk kapanis serisi (closes) uzerinden
        # hesaplanir, ekstra Yahoo istegi gerekmez.
        signal_macd = indicators.macd_signal(closes)
        rsi_val = indicators.rsi(closes)
        bb_percent = indicators.bollinger_percent_b(closes)
        adx_val = indicators.adx(hist)

        # Goreceli guc (RS): hissenin gunluk % degisimi ile BIST100 endeksinin
        # gunluk % degisimi arasindaki fark. Endeks +%4 oldugu bir gunde hisse
        # +%5 ise bu, endeksin duz oldugu bir gunde +%5 olmasindan cok daha az
        # anlamlidir - RS bu ayrimi yapar.
        benchmark_pct = get_benchmark_daily_pct()
        rel_strength = (
            daily_pct - benchmark_pct if (daily_pct == daily_pct and benchmark_pct is not None) else float("nan")
        )

        score_raw = indicators.confluence_score_raw(signal_1d, signal_macd, rsi_val, bb_percent, rel_strength)
        score = indicators.confluence_label(signal_1d, signal_macd, rsi_val, bb_percent, adx_val, rel_strength)

        # ATR bazli stop-loss/hedef: sinyalin yonune (AL/SAT) gore, hissenin
        # kendi oynakligina (ATR) olceklenmis gunluk (dar) ve swing (genis)
        # seviyeler. NOTR sinyalde net yon olmadigi icin (None, None) doner.
        atr_val = indicators.atr(hist)
        # Son 20 gunluk kapanis getirilerinin standart sapmasi - PAPER_MIN_VOL20
        # filtresi icin (bkz. bist_paper_trading.py). Zaten cekilmis olan 6 aylik
        # gunluk kapanis serisi (closes) uzerinden hesaplanir, ekstra Yahoo
        # istegi gerekmez.
        daily_returns = closes.pct_change().tail(20)
        vol20 = float(daily_returns.std()) if len(daily_returns.dropna()) >= 10 else float("nan")
        trade_direction = (
            "AL" if score in ("AL", "GUCLU_AL", "MUTLAK_AL")
            else "SAT" if score in ("SAT", "GUCLU_SAT", "MUTLAK_SAT")
            else None
        )
        day_stop, day_target = indicators.stop_target_levels(current_price, atr_val, trade_direction, ATR_DAY_MULT, ATR_TARGET_R)
        swing_stop, swing_target = indicators.stop_target_levels(
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
            "y1": y1_pct,
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
            "vol20": vol20,
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
    kez (gereksiz yere) tekrar cekilmesine yol aciyordu.

    **Backoff (2026-11-03'te eklendi):** ilk denemeden SONRAKI her retry
    'den ONCE RETRY_BACKOFF_SECONDS (3sn) beklenir - ilk deneme HEMEN
    calisir (gecikme YOK), sadece basarisizlik durumunda devreye girer.
    Eskiden retry'lar HICBIR bekleme olmadan, AYNI 12-thread'lik
    esazamanlilikla hemen tekrar deniyordu - Yahoo zaten rate-limit'teyken
    (429/Invalid Crumb) bu, durumu kotulestirebiliyordu (backoff yerine
    ayni yogunlukla tekrar tekrar vurmak). Kullanici isteginin ("Yahoo
    servisini zorlayan durumlari duzelt") bir parcasi."""
    rows_by_ticker = {}
    pending = list(dict.fromkeys(tickers))  # sirali + tekrarsiz

    for attempt in range(max_retries + 1):
        if not pending:
            break
        if attempt > 0:
            time.sleep(RETRY_BACKOFF_SECONDS)
        rows_by_ticker.update(fetch_all(pending, max_workers=max_workers))
        pending = [t for t in pending if t not in rows_by_ticker]

    return rows_by_ticker, pending
