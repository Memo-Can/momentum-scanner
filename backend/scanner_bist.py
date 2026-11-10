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
indikatorleri hesaplanip tek bir Skor'a (-5..+5, MUTLAK_AL..MUTLAK_SAT - bkz.
confluence_label) birlestirilir. Bu modul dogrudan calistirilmaz - terminal_bist.py
(terminal arayuzu) ve web_bist.py (web arayuzu) bu modulu
`import scanner_bist as scanner` ile kullanip kendi sunumlarini yapar.

Her BIST_REFRESH_SECONDS (300sn / 5dk) saniyede bir veri Yahoo Finance'ten
yeniden cekilir.

Onbellek: haftalik/aylik degisim her BIST_REFRESH_SECONDS'ta Yahoo'dan yeniden
cekilir (onbellek yok). Sadece BIST100 endeksi (RS icin) dongu basina
onbelleklenir - yoksa her hisse icin ayri ayri cekilip 100+ kat gereksiz
istege yol acardi. (2026-10-31'de kaldirilan 5 dakikalik EMA sinyali de
hisse basina AYRI bir Yahoo istegi gerektiriyordu - kaldirilmasi hisse
basina istek sayisini 3'ten 2'ye dusurdu.)

Sinyal gecmisi: Skoru MUTLAK_AL/GUCLU_AL/GUCLU_SAT/MUTLAK_SAT'a DONEN hisseler signal_log_bist.csv
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
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

# Calisma zamanlamasi (BIST_REFRESH_SECONDS, BIST acik saatleri) ARTIK
# burada TANIMLANMIYOR - app_settings.json'dan OKUNUP bu modulun isim
# alaninda sabit olarak disa veriliyor (scanner.BIST_REFRESH_SECONDS vb.) -
# .py yerine .json: duz veri, kod DEGIL, "yazilim standartlarina" uygun
# ayri bir ayar dosyasi. Anahtarlar BIST_ onekli - ileride baska bir
# piyasanin (orn. ABD/kripto) ayni dosyaya/benzer bir dosyaya kendi
# REFRESH_SECONDS/MARKET_OPEN_* degerleri eklenirse BIST'inkiyle
# KARISMASIN diye.
_APP_SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_settings.json")
with open(_APP_SETTINGS_PATH, "r", encoding="utf-8") as _f:
    _app_settings = json.load(_f)

BIST_REFRESH_SECONDS = _app_settings["BIST_REFRESH_SECONDS"]
BIST_MARKET_OPEN_HOUR = _app_settings["BIST_MARKET_OPEN_HOUR"]
BIST_MARKET_OPEN_MINUTE = _app_settings["BIST_MARKET_OPEN_MINUTE"]
BIST_MARKET_CLOSE_HOUR = _app_settings["BIST_MARKET_CLOSE_HOUR"]
BIST_MARKET_CLOSE_MINUTE = _app_settings["BIST_MARKET_CLOSE_MINUTE"]

# Telegram bot token/chat_id - sir asla koda/repoya yazilmaz, sadece ortam
# degiskeni (systemd servis dosyasinda Environment=, yerelde export). Ikisi
# de tanimli degilse _send_push_notifications() sessizce atlar - Telegram
# kurulmadan da tum tarama/skor mantigi normal calismaya devam eder.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

MAX_RETRIES = 2
TOP300_SIZE = 300  # BIST 300 sekmesi: gunluk % degisime gore ilk N (dinamik, endeks degil)


def is_market_open(now=None) -> bool:
    """BIST hisse tarama dongusunun (web_bist.py: _scan_loop(), bkz.
    oradaki cagri) Yahoo'ya istek atmasi icin "piyasa acik" sayilan an mi -
    hafta ici (Pazartesi-Cuma) VE app_settings.json'daki BIST_MARKET_OPEN_*/
    BIST_MARKET_CLOSE_* penceresi icindeyse True. Market seridi (doviz/emtia/
    kripto, web_bist.py: _market_scan_loop()) buna BAKMAZ - o
    enstrumanlar BIST disi saatlerde de islem goruyor.

    Resmi BIST tatilleri (bayram vb.) BILINCLI olarak HESABA KATILMAZ -
    o gunlerde tarama yine calisir ama Yahoo zaten degismeyen son fiyati
    dondurur, zararsiz bir fazladan istek olur. `now` parametresi test
    icin disaridan verilebilir, verilmezse gercek su an kullanilir."""
    now = now or datetime.now()
    if now.weekday() >= 5:  # 5=Cumartesi, 6=Pazar
        return False
    open_t = now.replace(hour=BIST_MARKET_OPEN_HOUR, minute=BIST_MARKET_OPEN_MINUTE, second=0, microsecond=0)
    close_t = now.replace(hour=BIST_MARKET_CLOSE_HOUR, minute=BIST_MARKET_CLOSE_MINUTE, second=0, microsecond=0)
    return open_t <= now <= close_t

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

# Yukaridaki iki listenin SON insan dogrulamasinin yapildigi tarih - endeks
# ceyreklik yeniden dengelendigi icin bu tarih eskiyince listelerin Borsa
# Istanbul'un resmi XU030/XU100 uyelik listesiyle tekrar karsilastirilmasi
# gerekir. Otomatik bir kaynaktan cekilmiyor (resmi API/scrape YOK, bilerek
# elle girilmis - bkz. yukaridaki yorumlar), bu yuzden otomatik
# GUNCELLENEMEZ; asagidaki kontrol SADECE servis her baslatildiginda bir
# konsol/journalctl hatirlatmasi basar.
BIST_INDEX_LISTS_VERIFIED_ON = "2026-09-01"


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

# Sanal para (paper trading) simulasyonu: GUCLU_AL'a YENI GECEN hisseler icin
# otomatik "satin alma", swing stop/hedef (asagida ATR_SWING_MULT/ATR_TARGET_R)
# ya da GUCLU_SAT'a donus ya da sure asimi ile "satma". Amac: Konfluens
# Skor'un gercekte bir edge saglayip saglamadigini kendi canli verimizle
# ampirik olcmek. _init_paper_trading() bu sabitlere ihtiyac duydugu icin
# (asagida cagriliyor) burada, DB kurulumundan ONCE tanimlanmalari gerekiyor.
PAPER_STARTING_BALANCE = 1_000_000.0
# Artik SADECE paper_account.max_positions sutununun varsayilan/ilk degeri -
# gercek calisma zamani degeri icin get_paper_max_positions()/DB'ye bak.
# Kullanici Sanal Al-Sat panelinden bunu degistirebiliyor (2026-11-06'da
# eklendi, bkz. CLAUDE.md ve set_paper_max_positions()).
PAPER_MAX_POSITIONS = 5
PAPER_MAX_HOLD_DAYS = 21  # ~15 islem gunune denk takvim gunu yaklasimi
# Otomatik alim icin minimum volatilite filtresi (2026-10-30, backtest sonucu
# eklendi): "row['vol20']" (gunluk kapanis getirilerinin son 20 gunluk standart
# sapmasi) bu esigin ALTINDAYSA GUCLU_AL gecisi otomatik alinmaz - 1 yillik
# backfill verisiyle yapilan test, en sakin %25'lik dilimdeki hisselerde
# GUCLU_AL'in pratikte edge tasimadigini (ortalama getiri ~%0.16, diger
# ceyreklerde %1.3-2.2) gosterdi. Esik, o testteki Q1 ustu (25. persentil)
# degeri (%2.13) - bkz. CLAUDE.md "GUCLU_AL volatilite filtresi - backtest".
# SADECE otomatik alim kararini etkiler - GUCLU_AL etiketi/gorunumu (sitede
# herkese gosterilen) DEGISMEZ, confluence_label() bundan habersiz.
PAPER_MIN_VOL20 = 0.0213
# Asagidaki 3 sabit SADECE paper_account.buy_criteria/sell_criteria/
# notify_criteria sutunlarinin CREATE/ALTER TABLE varsayilan degeri icin var -
# gecerli-deger kumeleri (PAPER_*_CRITERIA_OPTIONS) VE kullanicinin gercek
# okuma/yazma fonksiyonlari (get_paper_criteria()/set_paper_*_criteria())
# backend/settings.py'ye TASINDI (2026-11-06'da "Ayarlar" diye ayri bir
# ekrana/dosyaya ayrildi, bkz. CLAUDE.md). scanner_bist.py bu dosyayi
# HIC import ETMIYOR (settings.py'nin get_db_connection()/_DB_LOCK icin BU
# dosyaya baglanmasi yeterli - tersten bir import DAIRESEL olurdu). Gercek
# calisma zamani degeri icin her zaman DB'ye bak - run_paper_trading_cycle()/
# _send_push_notifications()/get_paper_summary() bu sutunlari DOGRUDAN SQL
# ile okur, bu sabitleri KULLANMAZ.
_STRONG_BUY_ZONE = ("GUCLU_AL", "MUTLAK_AL")
_DEFAULT_SELL_ZONE = ("SAT", "GUCLU_SAT", "MUTLAK_SAT", "STOP", "TARGET")
_DEFAULT_NOTIFY_ZONE = ("GUCLU_AL", "MUTLAK_AL")

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
                f"""
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

                CREATE TABLE IF NOT EXISTS last_known_score (
                    ticker TEXT PRIMARY KEY,
                    score TEXT NOT NULL,
                    timestamp TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS score_since (
                    ticker TEXT PRIMARY KEY,
                    score TEXT NOT NULL,
                    since TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS paper_account (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    cash REAL NOT NULL,
                    starting_balance REAL NOT NULL,
                    max_positions INTEGER NOT NULL DEFAULT {PAPER_MAX_POSITIONS},
                    buy_criteria TEXT NOT NULL DEFAULT '{",".join(_STRONG_BUY_ZONE)}',
                    sell_criteria TEXT NOT NULL DEFAULT '{",".join(_DEFAULT_SELL_ZONE)}',
                    notify_criteria TEXT NOT NULL DEFAULT '{",".join(_DEFAULT_NOTIFY_ZONE)}'
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

                CREATE TABLE IF NOT EXISTS paper_watchlist (
                    ticker TEXT PRIMARY KEY,
                    added_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ticker_flags (
                    ticker TEXT PRIMARY KEY,
                    flag TEXT,
                    note TEXT,
                    updated_at TEXT NOT NULL
                );
                """
            )
            conn.commit()

            # paper_trades 'source' sutunu olmadan DEPLOY EDILMIS olabilir
            # (manuel al-sat 2026-10'da eklendi) - CREATE TABLE IF NOT EXISTS
            # var olan bir tabloyu GUNCELLEMEZ, bu yuzden ayrica kontrol
            # edilip gerekirse eklenir. Mevcut satirlar (hepsi otomatik
            # sistemle acildi) varsayilan 'auto' degerini dogru alir.
            cols = [r["name"] for r in conn.execute("PRAGMA table_info(paper_trades)").fetchall()]
            if "source" not in cols:
                conn.execute("ALTER TABLE paper_trades ADD COLUMN source TEXT NOT NULL DEFAULT 'auto'")
                conn.commit()
            # 'note' sutunu - kapanan islemlere kullanicinin elle yazdigi
            # serbest metin aciklama (2026-11-05'te eklendi, bkz. CLAUDE.md).
            # Nullable - mevcut/yeni satirlar varsayilan NULL (aciklama yok),
            # frontend'de bos string olarak gosterilir.
            if "note" not in cols:
                conn.execute("ALTER TABLE paper_trades ADD COLUMN note TEXT")
                conn.commit()
            # 'max_positions' sutunu - kullanicinin elle degistirebildigi
            # maksimum acik pozisyon slotu (2026-11-06'da eklendi, bkz.
            # CLAUDE.md) - eskiden sabit PAPER_MAX_POSITIONS=5 idi. CREATE
            # TABLE IF NOT EXISTS var olan paper_account satirini (deploy
            # oncesi acilmis) GUNCELLEMEDIGI icin ayrica kontrol edilir.
            account_cols = [r["name"] for r in conn.execute("PRAGMA table_info(paper_account)").fetchall()]
            if "max_positions" not in account_cols:
                conn.execute(f"ALTER TABLE paper_account ADD COLUMN max_positions INTEGER NOT NULL DEFAULT {PAPER_MAX_POSITIONS}")
                conn.commit()
            # 'buy_criteria'/'sell_criteria' - kullanicinin elle secebildigi,
            # otomatik al/sat'i tetikleyen skor etiketleri (virgulle ayrilmis
            # TEXT, 2026-11-06'da eklendi, bkz. CLAUDE.md). Varsayilan deger
            # ESKI sabit davranisla (GUCLU_AL+MUTLAK_AL alim, SAT+GUCLU_SAT+
            # MUTLAK_SAT satis) AYNI - mevcut deploy'larda davranis DEGISMEZ.
            # ALTER TABLE ... DEFAULT bir SABIT ifade gerektirir, parametre
            # bind EDILEMEZ - degerler (_STRONG_BUY_ZONE/_DEFAULT_SELL_ZONE)
            # sabit, kod-ici bir listeden geldigi icin (kullanici girdisi
            # DEGIL) f-string ile gömmek güvenli.
            if "buy_criteria" not in account_cols:
                conn.execute(
                    f"ALTER TABLE paper_account ADD COLUMN buy_criteria TEXT NOT NULL DEFAULT '{','.join(_STRONG_BUY_ZONE)}'"
                )
                conn.commit()
            if "sell_criteria" not in account_cols:
                conn.execute(
                    f"ALTER TABLE paper_account ADD COLUMN sell_criteria TEXT NOT NULL DEFAULT '{','.join(_DEFAULT_SELL_ZONE)}'"
                )
                conn.commit()
            # 'notify_criteria' - Telegram SKOR sinyali bildirimini tetikleyen
            # etiketler, buy_criteria/sell_criteria'dan BAGIMSIZ (2026-11-06'da
            # kullanici istegiyle AYRILDI, bkz. CLAUDE.md). Varsayilan deger
            # ESKI (ayrilmadan ONCEki) spam-guvenli davranisla AYNI - SADECE
            # AL tarafi.
            if "notify_criteria" not in account_cols:
                conn.execute(
                    f"ALTER TABLE paper_account ADD COLUMN notify_criteria TEXT NOT NULL DEFAULT '{','.join(_DEFAULT_NOTIFY_ZONE)}'"
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

# Rate-limit/veri-eksikligi (Yahoo'dan bir veya daha fazla acik pozisyonun
# fiyatinin bu dongude gelmemesi) durumunda TEK bir Telegram uyarisi
# gonderilsin - _scan_health_alert_sent (web_bist.py) ile AYNI desen,
# sorun surerken her dongude (5dk'da bir) tekrar tekrar SPAM edilmesin, sorun
# duzelince (bir sonraki temiz dongude) tekrar False'a donup gelecekte
# yeniden tetiklenebilsin diye. bkz. run_paper_trading_cycle().
_paper_price_issue_alert_sent = False

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
    """BIST100 endeksinin gunluk yuzde degisimini BIST_REFRESH_SECONDS'a yakin
    bir sure onbellekten dondurur - her hisse icin ayri ayri degil, dongu basina
    bir kez cekilir (onbellek olmadan, 100+ hisse icin ayni endeks verisi
    100+ kez ayri ayri cekilirdi). Hisselerin gercekten piyasayla birlikte mi
    surukendigi yoksa piyasadan bagimsiz mi guclu oldugu (RS) bunun uzerinden
    hesaplanir."""
    cached = _benchmark_cache.get("pct")
    if cached and time.time() - cached[0] < BIST_REFRESH_SECONDS - 5:
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
        {"label": "BIST100", "price": xu100_price, "pct": xu100_pct, "unit": ""},
        {"label": "Dolar", "price": usd_price, "pct": usd_pct, "unit": "TL"},
        {"label": "Euro", "price": eur_price, "pct": eur_pct, "unit": "TL"},
        {"label": "Sterlin", "price": gbp_price, "pct": gbp_pct, "unit": "TL"},
        {"label": "Bitcoin", "price": btc_price, "pct": btc_pct, "unit": "$"},
        {"label": "Gram Altin", "price": gold_gram_price, "pct": gold_gram_pct, "unit": "TL"},
        {"label": "Gram Gumus", "price": silver_gram_price, "pct": silver_gram_pct, "unit": "TL"},
        {"label": "Petrol", "price": oil_price, "pct": oil_pct, "unit": "$"},
    ]


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
    ham deger ayrica saklanir - bkz. log_score_history.

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


def build_bist_query():
    """Yahoo'nun BIST (Borsa Istanbul) icin TUM piyasayi tarayan sorgusu -
    region="tr" butun BIST hisselerini kapsar, endeks uyeligi ayrimi/esik
    yoktur."""
    return yf.EquityQuery("eq", ["region", "tr"])


SCREEN_PAGE_SIZE = 250  # Yahoo'nun yf.screen() tek istekte donduregi maksimum sonuc sayisi


def fetch_all_bist_tickers():
    """Yahoo'nun region=tr taramasindaki TUM hisseleri sayfalama ile (tek
    istek max SCREEN_PAGE_SIZE donduruyor) toplar - hem canli tarama
    (terminal_bist.py/web_bist.py) hem de backfill_daily_bist.py
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
    icin kullanilir. web_bist.py: _scan_once() bunu, birlesik havuzun
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
            daily_pct = pct_change(current_price, prev_close)

        # Haftalik/aylik degisim, Yahoo'nun kendi haftalik/aylik mum verisindeki
        # (interval=1wk/1mo) donemin acilis fiyatina gore hesaplanir. Terminal
        # tablosunda gosterilmiyor, sadece web arayuzu icin.
        d5_pct = rolling_pct_change(closes, current_price, WEEKLY_LOOKBACK_DAYS)
        m1_pct = rolling_pct_change(closes, current_price, MONTHLY_LOOKBACK_DAYS)
        y1_pct = rolling_pct_change(closes, current_price, YEARLY_LOOKBACK_DAYS)

        # Gunluk EMA kesisimi: zaten cekilmis olan gunluk kapanis serisi (closes)
        # uzerinden hesaplanir, ekstra Yahoo istegi gerekmez. (2026-10-31'de
        # buradaki 5 dakikalik EMA bileseni - ayri bir Yahoo istegi gerektirdigi
        # ICIN - kaldirildi, bkz. CLAUDE.md "5 dakikalik bilesen kaldirildi":
        # hisse basina istek sayisini 3'ten 2'ye dusurup rate-limit riskini
        # azaltiyor, ayrica zaten gunluk mum canli fiyatla surekli guncellendigi
        # icin bilgi kaybi minimal.)
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

        score_raw = confluence_score_raw(signal_1d, signal_macd, rsi_val, bb_percent, rel_strength)
        score = confluence_label(signal_1d, signal_macd, rsi_val, bb_percent, adx_val, rel_strength)

        # ATR bazli stop-loss/hedef: sinyalin yonune (AL/SAT) gore, hissenin
        # kendi oynakligina (ATR) olceklenmis gunluk (dar) ve swing (genis)
        # seviyeler. NOTR sinyalde net yon olmadigi icin (None, None) doner.
        atr_val = atr(hist)
        # Son 20 gunluk kapanis getirilerinin standart sapmasi - PAPER_MIN_VOL20
        # filtresi icin (bkz. yukarida). Zaten cekilmis olan 6 aylik gunluk
        # kapanis serisi (closes) uzerinden hesaplanir, ekstra Yahoo istegi
        # gerekmez.
        daily_returns = closes.pct_change().tail(20)
        vol20 = float(daily_returns.std()) if len(daily_returns.dropna()) >= 10 else float("nan")
        trade_direction = (
            "AL" if score in ("AL", "GUCLU_AL", "MUTLAK_AL")
            else "SAT" if score in ("SAT", "GUCLU_SAT", "MUTLAK_SAT")
            else None
        )
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


RETRY_BACKOFF_SECONDS = 3.0  # her retry denemesi ONCESI bekleme suresi (bkz. asagida)


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


# Siralama onceligi: en guclu AL en ustte, en guclu SAT en altta - ayni
# skor grubunun icinde gunluk % buyukten kucuge sıralanir (web/terminal
# render sort_key'lerinde kullanilir).
# GUCLU_SAT artik canli taramada hic uretilmiyor (bkz. confluence_label) ama
# gecmis DB kayitlarinda/defansif olarak burada duruyor.
SCORE_ORDER = {"MUTLAK_AL": 0, "GUCLU_AL": 1, "AL": 2, "NOTR": 3, "SAT": 4, "GUCLU_SAT": 5, "MUTLAK_SAT": 6}


_last_logged_score = {}  # ticker -> log dosyasina en son yazilan skor


def _seed_last_logged_scores() -> None:
    """Process (re)basladiginda _last_logged_score'u her ticker'in SON
    bilinen skoruyla (last_known_score tablosundan, O(1) - buyuk
    score_history tablosu TARANMAZ) doldurur - boylece bir restart, zaten
    GUCLU_AL/SAT durumunda olan hisseleri YANLISLIKLA 'yeni gecis' sayip
    tekrar Telegram bildirimi GONDERMEZ. (Gercekten yasandi, 2026-10-28: ard
    arda deploy/restart'lar AKBNK gibi gunlerce GUCLU_SAT'ta kalan hisseleri
    her restart'ta yeniden bildirdi.)"""
    conn = get_db_connection()
    try:
        rows = conn.execute("SELECT ticker, score FROM last_known_score").fetchall()
        for row in rows:
            _last_logged_score[row["ticker"]] = row["score"]
    finally:
        conn.close()


_seed_last_logged_scores()


# ticker -> (score, since) - GUNCEL skorun NE ZAMANDAN BERI aktif oldugu
# (2026-11-05'te eklendi, bkz. CLAUDE.md "BIST sayfasinda sinyal zamani
# sutunu"). last_known_score'dan FARKLI: o her dongude upsert edilir (sadece
# "en son ne zaman gorduk" bilgisi), bu ise SADECE skor DEGISTIGINDE
# guncellenir - restart sonrasi bile "since" degeri DOGRU kalsin diye ayri
# bir tablo/cache.
_score_since_cache = {}


def _seed_score_since() -> None:
    """Process (re)basladiginda _score_since_cache'i score_since tablosundan
    doldurur - _seed_last_logged_scores() ile AYNI amac/desen (O(1), buyuk
    score_history taranmaz): restart, zaten uzun suredir ayni skorda olan
    hisseler icin "since" degerini YANLISLIKLA 'simdi' olarak sifirlamasin."""
    conn = get_db_connection()
    try:
        rows = conn.execute("SELECT ticker, score, since FROM score_since").fetchall()
        for row in rows:
            _score_since_cache[row["ticker"]] = (row["score"], row["since"])
    finally:
        conn.close()


_seed_score_since()


# Hisse isaretleri (guvenilir/sikintili + serbest metin not, 2026-11-09'da
# eklendi) - kullanici istegi: "bazi hisselerde sikintilar var, listede
# sikintili ya da guvenilir olan hisseleri bilmem gerekiyor." last_known_score/
# score_since ile AYNI "ticker basina tek satir, bellek-ici cache + DB" deseni.
# DB'ye DEGIL, sadece _ticker_flags_cache'e YAZMAK/OKUMAK bilerek ayrildi -
# bu veri DB'den BAGIMSIZ sik okunuyor (_run_scan() her dongude TUM hisseler
# icin okur), DB'ye her seferinde gitmek gereksiz.
TICKER_FLAG_OPTIONS = ("GUVENILIR", "SIKINTILI")
TICKER_NOTE_MAX_LEN = 250
_ticker_flags_cache = {}  # ticker -> (flag, note)


def _seed_ticker_flags() -> None:
    """Process (re)basladiginda _ticker_flags_cache'i ticker_flags
    tablosundan doldurur - _seed_score_since() ile AYNI O(1) desen."""
    conn = get_db_connection()
    try:
        rows = conn.execute("SELECT ticker, flag, note FROM ticker_flags").fetchall()
        for row in rows:
            _ticker_flags_cache[row["ticker"]] = (row["flag"], row["note"])
    finally:
        conn.close()


_seed_ticker_flags()


def get_all_ticker_flags() -> dict:
    """web_bist.py: _run_scan()'in HER satira isaret/not eklemek icin
    kullandigi toplu okuma - DB'ye GITMEZ, bellek-ici cache'in bir KOPYASINI
    doner (cagiran taraf yanlislikla mutasyona ugratmasin diye)."""
    return dict(_ticker_flags_cache)


def set_ticker_flag(ticker: str, flag, note) -> None:
    """Bir ticker'in isaretini/notunu DB'ye yazar VE ayni anda bellek-ici
    cache'i gunceller - web_bist.py'nin API route'u bu cagridan SONRA
    _state['rows']'daki ilgili satiri da DOGRUDAN mutasyona ugratip (bkz.
    orada) degisikligin bir sonraki tarama donguusunu (en fazla 5 dakika)
    beklemeden ANINDA yansimasini sagliyor.
    flag TICKER_FLAG_OPTIONS'ta degilse (bos string/None dahil) SESSIZCE
    None'a ("isaretsiz") dusurulur - API tarafinda ZATEN sikisi dogrulama
    var, bu fonksiyon DOGRUDAN cagrilsa bile guvenli kalsin diye settings.py'deki
    _clean_criteria_labels() ile AYNI savunmaci yaklasim. note cok uzunsa
    (TICKER_NOTE_MAX_LEN) ValueError firlatir - set_paper_trade_note() ile
    AYNI desen."""
    if flag not in TICKER_FLAG_OPTIONS:
        flag = None
    if note is not None and len(note) > TICKER_NOTE_MAX_LEN:
        raise ValueError(f"Not en fazla {TICKER_NOTE_MAX_LEN} karakter olabilir.")
    note = note or None
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _DB_LOCK:
        conn = get_db_connection()
        try:
            conn.execute(
                "INSERT INTO ticker_flags (ticker, flag, note, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(ticker) DO UPDATE SET flag=excluded.flag, note=excluded.note, updated_at=excluded.updated_at",
                (ticker, flag, note, now_str),
            )
            conn.commit()
        finally:
            conn.close()
    _ticker_flags_cache[ticker] = (flag, note)


def backfill_score_since() -> dict:
    """TEK SEFERLIK (idempotent, tekrar calistirilabilir) backfill -
    score_since ozelligi 2026-11-05'te EKLENDIGINDE tablo BOMBOS basladigi
    icin ILK dongude TUM ticker'lar yanlislikla "since=o an" aldi (gercek
    gecis anlari henuz bilinmiyordu - kullanici bunu fark edip "nötr'den
    al'a geçiş gibi GERÇEK degisim anini yazmali" diye bildirdi). Bu
    fonksiyon score_history'deki (SADECE source='live' - backfill'in GUNLUK
    tek-ornegi gercek saat/dakika hassasiyetini TEMSIL ETMIYOR, bu yuzden
    KASITLI disarida birakildi) gecmis veriyi her ticker icin EN YENIDEN
    geriye dogru tarayip GUNCEL skorun GERCEKTEN ne zamandan beri (ayni
    skorla kesintisiz) aktif oldugunu hesaplar, score_since tablosunu bu
    DOGRU degerlerle UZERINE YAZAR ve bellek-ici _score_since_cache'i de
    gunceller. Yahoo'ya hic istek ATMAZ, sadece yerel DB'yi okur/yazar.
    Canli veri Eylul 2026'dan beri biriktigi icin (bkz. CLAUDE.md) bu
    tarihten ONCE baslamis bir skorun GERCEK baslangici bilinemez - o
    durumda elimizdeki EN ESKI canli kaydin zamani kullanilir (olduğundan
    KISA gosterir, olduğundan UZUN asla gostermez)."""
    conn = get_db_connection()
    try:
        tickers = [
            r["ticker"]
            for r in conn.execute("SELECT DISTINCT ticker FROM score_history WHERE source='live'").fetchall()
        ]
        updates = []
        for ticker in tickers:
            rows = conn.execute(
                "SELECT timestamp, score FROM score_history WHERE ticker = ? AND source = 'live' "
                "ORDER BY timestamp DESC",
                (ticker,),
            ).fetchall()
            if not rows:
                continue
            current_score = rows[0]["score"]
            since = rows[0]["timestamp"]
            for row in rows[1:]:
                if row["score"] != current_score:
                    break
                since = row["timestamp"]
            updates.append({"ticker": ticker, "score": current_score, "since": since})
    finally:
        conn.close()

    with _DB_LOCK:
        conn = get_db_connection()
        try:
            conn.executemany(
                """INSERT INTO score_since (ticker, score, since) VALUES (:ticker, :score, :since)
                   ON CONFLICT(ticker) DO UPDATE SET score=excluded.score, since=excluded.since""",
                updates,
            )
            conn.commit()
        finally:
            conn.close()

    for u in updates:
        _score_since_cache[u["ticker"]] = (u["score"], u["since"])

    return {"tickers_updated": len(updates)}


def _seed_paper_last_score() -> None:
    """Process (re)basladiginda _paper_last_score'u last_known_score
    tablosundan doldurur - _seed_last_logged_scores() ile AYNI amac/desen
    (restart'ta zaten GUCLU_AL/MUTLAK_AL durumunda olan hisseler YANLISLIKLA
    'yeni gecis' sayilip otomatik satin ALINMASIN). Eskiden _paper_last_score
    her restart'ta BOMBOS basliyordu, sadece "ilk dongude prev=None, hicbir
    sey tetiklenmez" korumasina guveniliyordu - ama bu korumanin YAN ETKISI,
    dinamik BIST 300 havuzuna HER GUN yeni giren bir hissenin (ilk kez
    gorulduyor, prev=None) ZATEN GUCLU_AL/MUTLAK_AL olarak havuza girmesi
    durumunda SONSUZA KADAR atlanmasiydi (sadece restart'ta degil, HER yeni
    havuz girisinde) - 2026-11-02'de gercekten yasandi/bulundu (bkz.
    CLAUDE.md). last_known_score'dan seed etmek restart korumasini ayni
    sekilde saglarken, gercekten YENI (hic gorulmemis) bir hisseyi artik
    kisitlamiyor - bkz. asagidaki run_paper_trading_cycle'daki degisiklik."""
    conn = get_db_connection()
    try:
        rows = conn.execute("SELECT ticker, score FROM last_known_score").fetchall()
        for row in rows:
            _paper_last_score[row["ticker"]] = row["score"]
    finally:
        conn.close()


_seed_paper_last_score()


def _telegram_send(text: str) -> None:
    """Telegram bot API'sine HAM bir metin mesaji gonderir - sinyal
    bildirimleri (_send_push_notifications) VE sanal al-sat alim/satim
    bildirimlerinin (_send_paper_trade_notification) ORTAK gonderim kodu.
    TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID tanimli degilse (henuz kurulmadiysa)
    sessizce hicbir sey yapmaz, gonderim hatasi exception firlatmadan
    print() ile loglanir - cagiran taraf (tarama/paper trading dongusu)
    bundan etkilenmemeli."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
        )
        urllib.request.urlopen(req, timeout=10).close()
    except Exception as e:
        print("Telegram bildirimi gonderilemedi:", e)


# Saatlik K/Z raporundaki her pozisyonun YANINA "GÜÇLÜ AL (3 gün önce)" gibi
# bir baglam eklemek icin (2026-11-06'da eklendi, kullanici istegi: "projeye
# ne eklenebilir" sorusuna yanit olarak secilen madde) - 7 skorun TAMAMI icin
# okunabilir TR etiket. _SIGNAL_NOTIFY_LABELS'tan FARKLI/AYRI tutuldu cunku o
# SADECE guclu 4 etiketi kapsiyor (sinyal bildirimi baglaminda yeterliydi),
# burada AL/NOTR/SAT de gerekiyor.
_SCORE_TEXT_LABELS = {
    "MUTLAK_AL": "MUTLAK AL",
    "GUCLU_AL": "GÜÇLÜ AL",
    "AL": "AL",
    "NOTR": "NÖTR",
    "SAT": "SAT",
    "GUCLU_SAT": "GÜÇLÜ SAT",
    "MUTLAK_SAT": "MUTLAK SAT",
}


def _format_relative_tr(since) -> str:
    """frontend/templates/index.html: fmtRelativeTime()'in Python karsiligi -
    Telegram DUZ METIN raporu icin ayni "3 gün önce"/"2 saat önce"/
    "5 dakika önce"/"az önce" bicimini uretir. since None/parse edilemezse
    '-' doner (score_since henuz hesaplanmamis - orn. iskelet satirlar)."""
    if not since:
        return "-"
    try:
        then = datetime.strptime(since, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return "-"
    diff_min = int((datetime.now() - then).total_seconds() // 60)
    if diff_min < 1:
        return "az önce"
    if diff_min < 60:
        return f"{diff_min} dakika önce"
    diff_hour = diff_min // 60
    if diff_hour < 24:
        return f"{diff_hour} saat önce"
    diff_day = diff_hour // 24
    return f"{diff_day} gün önce"


def build_paper_hourly_report(rows) -> str:
    """Su an acik olan TUM sanal pozisyonlari (kaynak farki YOK, auto+manual)
    rows icindeki (zaten taranmis, EKSTRA Yahoo istegi YOK) canli fiyatla
    birlestirip saat basi bir K/Z rapor metni olusturur. Hem
    web_bist.py: _maybe_send_hourly_paper_report() (gercek saatlik
    tetikleyici, bkz. orada) HEM DE /api/paper/report-test (anlik test
    tetikleyicisi) tarafindan cagirilir - ikisi de AYNI formati gorsun diye
    rapor metni ve gonderimi (send_paper_hourly_report) ayri tutuldu.

    Her pozisyonun yanina GUNCEL skoru ve bu skorun NE ZAMANDAN BERI aktif
    oldugunu (rows zaten log_score_history()'den GECTIGI icin her satirda
    row["score"]/row["score_since"] hazir - EK bir Yahoo/DB sorgusu
    GEREKMEZ) ekler - 2026-11-06'da eklendi."""
    price_by_ticker = {r["ticker"]: r["price"] for r in rows}
    score_by_ticker = {r["ticker"]: r.get("score") for r in rows}
    score_since_by_ticker = {r["ticker"]: r.get("score_since") for r in rows}
    positions = sorted(get_paper_positions(), key=lambda p: p["entry_time"])
    summary = get_paper_summary()

    lines = [f"📊 Saatlik Sanal Al-Sat Raporu ({datetime.now().strftime('%Y-%m-%d %H:%M')})"]
    missing_price_tickers = []
    if not positions:
        lines.append("Açık pozisyon yok.")
    else:
        for p in positions:
            source_label = "Otomatik" if p["source"] == "auto" else "Manuel"
            price = price_by_ticker.get(p["ticker"])
            score = score_by_ticker.get(p["ticker"])
            score_label = _SCORE_TEXT_LABELS.get(score, score)
            since_text = _format_relative_tr(score_since_by_ticker.get(p["ticker"]))
            signal_info = f" - {score_label} ({since_text})" if score else ""
            if price is not None and price == price:  # NaN degilse
                pnl = (price - p["entry_price"]) * p["shares"]
                pnl_pct = (price / p["entry_price"] - 1.0) * 100.0
                symbol = "📈" if pnl >= 0 else "📉"
                lines.append(
                    f"{symbol} {p['ticker']} ({source_label}): {pnl:+.0f} TL ({pnl_pct:+.2f}%) "
                    f"[{p['entry_price']:.2f} -> {price:.2f} TL]{signal_info}"
                )
            else:
                lines.append(f"⚪ {p['ticker']} ({source_label}): güncel fiyat yok")
                missing_price_tickers.append(p["ticker"])

    # Fiyati gelmeyen pozisyon(lar) varsa (rate limit/veri sorunu) rapora
    # bunu ACIKCA belirten bir uyari eklenir - kullanici "telegram mesajinda
    # da sorun oldugunu belirtsin" dedi (2026-11-04). run_paper_trading_cycle()
    # bu durumda zaten etkilenen pozisyonlarin K/Z'sini NOTR (0 degisim)
    # kabul ettigi icin asagidaki Toplam K/Z de yanlislikla cokmez, ama
    # kullanici yine de verinin eksik oldugunu bu raporda gormeli.
    if missing_price_tickers:
        lines.append("")
        lines.append(
            f"⚠️ Not: {', '.join(missing_price_tickers)} için fiyat verisi alınamadı "
            "(rate limit veya başka bir veri sorunu olabilir) - bu pozisyon(lar)ın "
            "K/Z'si bu raporda güncellenmedi."
        )

    lines.append("")
    lines.append(
        f"Toplam {summary['open_positions']} açık pozisyon | Nakit {summary['cash']:.0f} TL | "
        f"Equity {summary['equity']:.0f} TL | Toplam K/Z {summary['total_pnl']:+.0f} TL"
    )
    return "\n".join(lines)


def send_paper_hourly_report(rows) -> str:
    """build_paper_hourly_report()'u olusturup Telegram'a gonderir, metni
    de geri dondurur - cagiran taraf (test endpoint'i) Telegram kurulu
    olmasa da ne gonderilecegini gorebilsin diye."""
    text = build_paper_hourly_report(rows)
    _telegram_send(text)
    return text


def _format_pct_tr(v) -> str:
    """fmtPct()'in (frontend) DUZ METIN karsiligi - Telegram raporu icin.
    None/NaN icin '-' doner."""
    if v is None or v != v:
        return "-"
    return f"{v:+.2f}%"


def build_paper_weekly_report() -> str:
    """Haftada bir (ilk tarama gunu) gonderilen, Sanal Al-Sat equity
    performans metriklerini (bkz. CLAUDE.md "Equity performans metrikleri")
    VE BIST100 benchmark karsilastirmasini (bkz. "BIST100 benchmark
    karsilastirmasi") ozetleyen bir TELEGRAM mesaji - saatlik K/Z raporunun
    (ANLIK acik pozisyon durumu) AKSINE, bu rapor get_paper_summary()'nin
    dondurdugu "simdiye kadarki TUM surec" penceresindeki performansi bir
    bakista gosterir - haftaya OZEL bir pencere hesaplanmiyor (Sharpe/
    drawdown gibi metrikler zaten tum sureci kapsiyor, haftalik olan SADECE
    bu raporun GONDERIM SIKLIGI)."""
    s = get_paper_summary()
    total_pct = (s["total_pnl"] / s["starting_balance"] * 100.0) if s["starting_balance"] else None
    win_rate_text = "-" if s["win_rate"] is None else f"%{s['win_rate']:.0f}"
    drawdown_pct_text = _format_pct_tr(-s["max_drawdown_pct"]) if s["max_drawdown_pct"] else _format_pct_tr(0.0)
    sharpe_text = "-" if s["sharpe_ratio"] is None else f"{s['sharpe_ratio']:.2f}"
    profit_factor_text = "-" if s["profit_factor"] is None else f"{s['profit_factor']:.2f}x"
    avg_win_text = "-" if s["avg_win"] is None else f"{s['avg_win']:+,.0f} TL"
    avg_loss_text = "-" if s["avg_loss"] is None else f"{s['avg_loss']:+,.0f} TL"
    lines = [
        f"📈 Haftalık Performans Raporu ({datetime.now().strftime('%Y-%m-%d')})",
        "",
        f"Toplam Değer: {s['equity']:,.0f} TL",
        f"Toplam K/Z: {s['total_pnl']:+,.0f} TL ({_format_pct_tr(total_pct)})",
        f"Açık Pozisyon: {s['open_positions']} / {s['max_positions']} | Kapanan İşlem: {s['closed_trades']} | Kazanma Oranı: {win_rate_text}",
        "",
        f"Maks. Drawdown: {s['max_drawdown']:,.0f} TL ({drawdown_pct_text})",
        f"Sharpe Oranı: {sharpe_text}",
        f"Profit Factor: {profit_factor_text}",
        f"Ort. Kazanç/Kayıp: {avg_win_text} / {avg_loss_text}",
        "",
        f"BIST100 (Aynı Dönem): {_format_pct_tr(s['benchmark_return_pct'])}",
        f"Alfa (BIST100'e Göre): {_format_pct_tr(s['alpha_pct'])}",
    ]
    return "\n".join(lines)


def send_paper_weekly_report() -> str:
    """build_paper_weekly_report()'u olusturup Telegram'a gonderir, metni
    de geri dondurur - send_paper_hourly_report() ile AYNI desen (test
    endpoint'i Telegram kurulu olmasa da metni gorebilsin diye)."""
    text = build_paper_weekly_report()
    _telegram_send(text)
    return text


def send_health_alert(text: str) -> None:
    """Tarama dongusunun kendi kendini izleyen basit saglik uyarisi -
    web_bist.py: _scan_loop() ardisik birkac basarisiz _scan_once()
    denemesinden sonra bunu cagirir (bkz. oradaki _SCAN_FAILURE_ALERT_THRESHOLD).
    _telegram_send ile AYNI alt yapiyi kullanir (token/chat_id tanimli
    degilse sessizce atlar) - amac sadece "servis sessizce veri cekmeyi
    durdurdu, kimse haberdar degil" durumunu yakalamak, 5 dakikalik Yahoo
    rate-limit dalgalanmalarini degil (tek seferlik hatalar bunu TETIKLEMEZ,
    sadece UST USTE ayni dongude basarisizlik tetikler)."""
    _telegram_send(f"⚠️ {text}")


_SIGNAL_NOTIFY_LABELS = {
    "GUCLU_AL": "GÜÇLÜ AL",
    "MUTLAK_AL": "MUTLAK AL",
    "GUCLU_SAT": "GÜÇLÜ SAT",
    "MUTLAK_SAT": "MUTLAK SAT",
}


def _send_push_notifications(entries) -> None:
    """entries (log_signal_changes'in az once tespit ettigi MUTLAK_AL/
    GUCLU_AL/GUCLU_SAT/MUTLAK_SAT gecisleri) icin Telegram'a bir mesaj
    gonderir - SADECE kullanicinin notify_criteria'sinda ISARETLI olan
    etiketler icin (2026-11-06'da eklendi, bkz. CLAUDE.md). **Bu, otomatik
    alim/satim kriterinden (buy_criteria/sell_criteria) TAMAMEN BAGIMSIZ**
    (ayni gun ikinci bir degisiklikle AYRILDI - kullanici istegi: "otomatik
    alim satim kriterleriyle telegram mesaj kriterlerini birbirinden
    ayiralim") - bir hisseyi otomatik ALMAYA/SATMAYA karar vermekle o
    hisse icin Telegram bildirimi almak artik birbirine BAGLI DEGIL, iki
    ayri ayar. Varsayilan notify_criteria SADECE AL tarafi (GUCLU_AL/
    MUTLAK_AL) - SAT tarafi (GUCLU_SAT/MUTLAK_SAT) varsayilan KAPALI (eski,
    2026-10-30'dan kalma spam-guvenli davranisla AYNI)."""
    # Sanal Al-Sat'ta (kaynak auto/manual farki yok) ZATEN ACIK pozisyonu
    # olan hisseler icin GUCLU_AL/MUTLAK_AL bildirimi tekrar gonderilmez
    # (2026-11-02, kullanici istegi) - "elimde zaten var" bilgisini tekrar
    # tekrar Telegram'a bildirmenin anlami yok, ilk alim aninda ZATEN ayrica
    # bir "🛒 SANAL ALIM" bildirimi gonderiliyor (bkz. _send_paper_trade_notification).
    # Bu kontrol log_signal_changes()'in signal_log'a YAZMASINI etkilemez -
    # sadece push gonderimi atlanir, tarihsel kayit kesintisiz devam eder.
    # Not: run_paper_trading_cycle() bu fonksiyondan SONRA cagrildigi icin
    # (bkz. web_bist.py: _scan_once()) burada okunan pozisyon listesi
    # bu dongude YENI acilan bir alimi henuz icermez - yani bir hisse GUCLU_AL/
    # MUTLAK_AL'a ILK girdigi dongude (tam alim tetiklendigi an) bildirim HALA
    # gonderilir, SADECE zaten ONCEKI bir dongude acilmis pozisyonlar icin
    # bastirilir.
    held_tickers = {p["ticker"] for p in get_paper_positions()}
    # notify_criteria DOGRUDAN SQL ile okunuyor - get_paper_criteria()
    # backend/settings.py'ye TASINDI (2026-11-06, Ayarlar ekrani ayrilmasi,
    # bkz. CLAUDE.md), scanner_bist.py bu modulu DAIRESEL IMPORT
    # olusturmamak icin HIC import etmiyor (settings.py bu dosyadaki
    # get_db_connection()/_DB_LOCK'u kullaniyor, yani bagimlilik TEK yonlu).
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT notify_criteria FROM paper_account WHERE id = 1").fetchone()
        notify_zone = set(row["notify_criteria"].split(",")) if row and row["notify_criteria"] else set()
    finally:
        conn.close()
    for entry in entries:
        if entry["score"] not in notify_zone:
            continue
        if entry["ticker"] in held_tickers:
            continue
        # Telegram mesajlarinda gercek metin rengi desteklenmiyor - yesil/kirmizi
        # algisi icin renkli daire emoji kullanilir (AL taraf=yesil, SAT taraf=kirmizi).
        symbol = "🟢" if entry["score"] in ("GUCLU_AL", "MUTLAK_AL") else "🔴"
        label = _SIGNAL_NOTIFY_LABELS.get(entry["score"], entry["score"])
        text = (
            f"{symbol} {entry['ticker']} -> {label}\n"
            f"{entry['price']:.2f} TL ({entry['daily_pct']:+.2f}%)\n"
            f"{entry['timestamp']}"
        )
        _telegram_send(text)


_PAPER_EXIT_REASON_LABELS = {
    "stop": "Stop",
    "target": "Hedef",
    "signal_reversal": "Sinyal Tersi",
    "time_stop": "Süre Aşımı",
    "manual_sat_signal": "Sinyal (SAT)",
}


def _send_paper_trade_notification(event: dict) -> None:
    """Otomatik Sanal Al-Sat alim/satim olaylarini (run_paper_trading_cycle'in
    az once yaptigi) Telegram'a bildirir. Sinyal bildirimleri (🟢/🔴 daire)
    ile KARISTIRILMASIN diye BILINCLI olarak FARKLI bir ikon ailesi kullanilir
    (2026-10-28'de, sepet+daire denemesi sinyallerle ayni gorundugu icin
    degistirildi): alim = sepet (giris, henuz iyi/kotu degil), satis =
    kar/zarara gore yukari/asagi grafik oku (trading botlarinda kar/zarar
    yonu icin yaygin kullanilan ikon). ⚠️ Docstring DUZELTMESI (2026-11-06,
    eskiden burada "SADECE source='auto' icin cagirilir" yaziyordu ama bu
    HICBIR ZAMAN dogru olmamisti - trade_events source'a gore filtrelenmez,
    hem auto hem manual alim/satim bildiriliyor (2026-10-31'de manuel
    SAT-sinyali satislari da dahil edildi, bkz. CLAUDE.md "Bildirimler").
    SADECE kullanicinin manual_sell() ile DOGRUDAN elle sattigi islemler bu
    akisin DISINDA - cunku kullanici zaten ne yaptigini biliyor."""
    if event["kind"] == "buy":
        text = (
            f"🛒 SANAL ALIM: {event['ticker']}\n"
            f"{event['price']:.2f} TL x {event['shares']:.2f} adet ({event['allocation']:.0f} TL)\n"
            f"{event['timestamp']}"
        )
    else:
        pnl = event["pnl"]
        symbol = "📈" if pnl >= 0 else "📉"
        sonuc = "KAR" if pnl >= 0 else "ZARAR"
        reason = _PAPER_EXIT_REASON_LABELS.get(event["exit_reason"], event["exit_reason"])
        text = (
            f"{symbol} SANAL SATIŞ ({sonuc}): {event['ticker']}\n"
            f"Giriş {event['entry_price']:.2f} -> Çıkış {event['exit_price']:.2f} TL\n"
            f"K/Z: {pnl:+.0f} TL - Neden: {reason}\n"
            f"{event['timestamp']}"
        )
    _telegram_send(text)


_STRONG_SIGNAL_LABELS = ("GUCLU_AL", "MUTLAK_AL", "GUCLU_SAT", "MUTLAK_SAT")


def log_signal_changes(rows):
    """Skoru MUTLAK_AL/GUCLU_AL/GUCLU_SAT/MUTLAK_SAT'a DONEN (onceki dongude
    farkli olan) hisseleri signal_log tablosuna ekler - GUCLU_AL<->MUTLAK_AL
    (ya da GUCLU_SAT<->MUTLAK_SAT) arasindaki ESKALASYON/DUSUS de "degisim"
    sayilir, zira skor degismis olur. Her dongude ayni sinyali tekrar tekrar
    yazmaz - sadece durum degistiginde kaydeder, boylece log tablosu anlamli
    kalir ve her satir gercekten "yeni bir sinyal aninı" temsil eder."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    new_entries = []
    for row in rows:
        ticker = row["ticker"]
        score = row["score"]
        prev = _last_logged_score.get(ticker)
        if score != prev and score in _STRONG_SIGNAL_LABELS:
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
    ham veri) kaynagi budur - BIST_REFRESH_SECONDS zaten 5 dakika oldugu icin her
    dongu dogal olarak bir "5 dakikalik" ornek temsil eder.

    Ayrica (2026-11-05'te eklendi) her satirin GUNCEL skorunun NE ZAMANDAN
    BERI aktif oldugunu _score_since_cache/score_since tablosundan hesaplayip
    satirin KENDISINE row["score_since"] ("YYYY-MM-DD HH:MM:SS") olarak
    ekler - rows zaten _run_scan()'daki top_rows ile AYNI liste/dict
    nesneleri oldugu icin burada yapilan mutasyon _state["rows"]'a dogrudan
    yansir, web_bist.py'de EK bir degisiklik gerekmez. Skor
    DEGISMEDIYSE onceki "since" korunur, DEGISTIYSE bu dongunun zamanina
    sifirlanir VE SADECE degisen ticker'lar icin score_since tablosuna
    yazilir (degismeyenler icin gereksiz yazim yapilmaz, last_known_score'un
    aksine)."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entries = []
    since_changes = []
    for row in rows:
        ticker = row["ticker"]
        score = row["score"]
        cached = _score_since_cache.get(ticker)
        if cached is None or cached[0] != score:
            since = now
            _score_since_cache[ticker] = (score, since)
            since_changes.append({"ticker": ticker, "score": score, "since": since})
        else:
            since = cached[1]
        row["score_since"] = since
        entries.append(
            {
                "timestamp": now,
                "ticker": ticker,
                "score_raw": row["score_raw"],
                "score": score,
                "price": row["price"],
            }
        )
    with _DB_LOCK:
        conn = get_db_connection()
        try:
            conn.executemany(
                """INSERT INTO score_history (timestamp, ticker, score_raw, score, price, source)
                   VALUES (:timestamp, :ticker, :score_raw, :score, :price, 'live')""",
                entries,
            )
            # last_known_score: her ticker icin TEK satir (upsert) - buyuyen
            # score_history'yi taramadan "bu ticker su an hangi skorda" sorusuna
            # O(1) cevap vermek icin (bkz. _seed_last_logged_scores()).
            conn.executemany(
                """INSERT INTO last_known_score (ticker, score, timestamp)
                   VALUES (:ticker, :score, :timestamp)
                   ON CONFLICT(ticker) DO UPDATE SET score=excluded.score, timestamp=excluded.timestamp""",
                entries,
            )
            if since_changes:
                conn.executemany(
                    """INSERT INTO score_since (ticker, score, since)
                       VALUES (:ticker, :score, :since)
                       ON CONFLICT(ticker) DO UPDATE SET score=excluded.score, since=excluded.since""",
                    since_changes,
                )
            conn.commit()
        finally:
            conn.close()


def get_score_history(ticker: str, since_timestamp: str):
    """Verilen ticker icin since_timestamp'ten (dahil, 'YYYY-MM-DD HH:MM:SS')
    itibaren TUM skor gecmisi satirlarini (hem canli hem backfill kaynakli,
    'source' sutunuyla ayristirilir) zaman sirasiyla dondurur. Eskiden iki
    ayri bellek-ici index'ten (bkz. gecmis web_bist.py) okunurdu; artik
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
    web_bist.py: _scan_once() tarafindan HER tarama dongusunde
    (BIST_REFRESH_SECONDS) cagrilir, ayni rows (zaten taranmis, Yahoo'ya
    tekrar gidilmez) kullanilir. Cagiran taraf bunu kendi try/except'i
    icinde sarmali - burada beklenmeyen bir hata ana tarama/skor
    loglama akisini bozmamali.

    1) Acik pozisyonlari cikis kuralina gore kapatir. 'auto' pozisyonlar
       icin 4 kural (stop/hedef/PAPER_MAX_HOLD_DAYS sure asimi/sinyal
       tersi) - bu 3 ek kural 2026-11-02'de kullanici istegiyle kaldirilip
       2026-11-06'da GERI eklendi, bkz. CLAUDE.md. stop/hedef kurallarinin
       IKISI de artik kullanicinin sell_criteria'sindan ayri ayri
       ac/kapa yapilabiliyor ("STOP"/"TARGET" sahte etiketleri, 2026-11-06'da
       eklendi) - kapatilirsa o mekanizma hic calismaz. 'manual' pozisyonlar
       icin stop_price/target_price zaten hep NULL oldugundan (manual_buy())
       SADECE sinyal tersi kurali gecerli - bu hic degismedi.
    2) rows'taki YENI GUCLU_AL/MUTLAK_AL gecislerini (_paper_last_score ile
       karsilastirarak, ikisi TEK bir "guclu alim bolgesi" sayilir) tespit
       eder, zaten ACIK bir pozisyonu (source farki YOK) olan ticker'lari
       atlayarak acik slot kadarini sirayla satin alir. Acik slot sayisi
       ARTIK TOPLAM acik pozisyon sayisina (auto+manual, 2026-11-03'te
       degistirildi - bkz. asagidaki yorum) gore hesaplanir - toplam
       max_positions (artik sabit 5 DEGIL, kullanici tarafindan
       degistirilebilir, bkz. get_paper_max_positions()/CLAUDE.md,
       2026-11-06) kadar aciksa HICBIR yeni alim yapilmaz. Pozisyon
       buyuklugu SABIT bir deger DEGIL - her alimda o anki GUNCEL toplam
       deger (nakit + acik pozisyonlarin o anki degeri) max_positions'a
       bolunerek hesaplanir (2026-11-02'de kullanici istegiyle
       degistirildi, bkz. CLAUDE.md).
    3) Her dongude bir equity (nakit + acik pozisyon degeri) satiri
       loglar - bir pozisyonun fiyati bu dongude gelmediyse (rate limit/veri
       sorunu) o pozisyon SIFIR degil kendi allocation'i (K/Z degisimi NOTR)
       ile sayilir, boylece equity'ye yanlislikla cokmus bir deger YAZILMAZ;
       bu durumda ayrica (sorun surerken TEK seferlik) bir Telegram uyarisi
       gonderilir - bkz. asagidaki price_data_issue/_paper_price_issue_alert_sent."""
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    rows_by_ticker = {r["ticker"]: r for r in rows}
    # Telegram bildirimleri DB kilidi/commit'inden SONRA (asagida, with
    # bloğu disinda) gonderilir - log_signal_changes'teki AYNI sebep: ag
    # istegi _DB_LOCK'u gereksiz yere uzun tutmasin, gonderim basarisiz
    # olsa bile islem her zaman DB'ye yazilmis olsun.
    trade_events = []

    with _DB_LOCK:
        conn = get_db_connection()
        try:
            account_row = conn.execute(
                "SELECT cash, max_positions, buy_criteria, sell_criteria FROM paper_account WHERE id = 1"
            ).fetchone()
            cash = account_row["cash"]
            max_positions = account_row["max_positions"]
            # Kullanicinin elle sectigi (2026-11-06'da eklendi, bkz. CLAUDE.md)
            # otomatik alim/satim kriterleri - bos string (hic secim yok)
            # bos set'e donusur, "in" kontrolu dogal olarak hep False olur.
            buy_zone = set(account_row["buy_criteria"].split(",")) if account_row["buy_criteria"] else set()
            sell_zone = set(account_row["sell_criteria"].split(",")) if account_row["sell_criteria"] else set()
            # 'auto' pozisyonlar icin stop/hedef/sure asimi/sinyal tersi
            # (4 kural, oncelik sirasi: stop > hedef > sure asimi > sinyal) -
            # 2026-11-02'de kaldirilip 2026-11-06'da kullanici istegiyle GERI
            # eklendi (bkz. CLAUDE.md). 'manual' pozisyonlar da 2026-11-10'dan
            # itibaren (kullanici istegi: "manuel acik pozisyonlarda hedef al
            # sat neden yok" -> "otomatik hesapla") manual_buy()'dan GERCEK
            # stop_price/target_price alabiliyor - asagidaki stop/hedef
            # kontrolleri source'tan BAGIMSIZ (sadece "is not None" sartiyla)
            # oldugu icin burada HICBIR degisiklik gerekmedi, sadece ATR
            # verisi yetersizse (eski davranis gibi) NULL kalip bu ikisi
            # etkisiz olur. Sure asimi (time_stop) ise HALA SADECE 'auto' -
            # kullanici sadece stop/hedefi geri istedi. Kullanici "Sat"
            # butonuyla manuel pozisyonu HER ZAMAN elle de kapatabilir
            # (manual_sell()), bu degismedi.
            open_positions = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM paper_trades WHERE status = 'open'"
                ).fetchall()
            ]

            closed_this_cycle = set()
            for pos in open_positions:
                row = rows_by_ticker.get(pos["ticker"])
                if row is None:
                    continue  # bu dongude veri gelmedi (delisted/gecici) - dokunma

                price = row["price"]
                exit_reason = None
                # "Sorunlu" (SIKINTILI) isaretli bir hissede OTOMATIK satim da
                # tamamen durur (2026-11-10'da eklendi, kullanici istegi:
                # "hisse sorunlu olarak isaretlendiyse otomatik alim satim
                # yapmasin" - AskUserQuestion ile netlestirildi: kaynak
                # (auto/manual) farki YOK, ikisi de kapsanir). Otomatik alim
                # zaten candidate dongusunde ayriyeten engelleniyor (bkz.
                # yukarida). Kullanici "Sat" butonuyla (manual_sell()) HER
                # ZAMAN elle kapatabilir - bu kontrolden GECMEZ, bu yuzden
                # etkilenmez.
                if row.get("ticker_flag") == "SIKINTILI":
                    pass
                # "STOP"/"TARGET" kullanicinin sell_criteria'dan kaldirabildigi
                # SAHTE etiketler (2026-11-06'da eklendi) - kapatilirsa o
                # mekanizma TAMAMEN devre disi kalir (auto/manual farki yok -
                # manual_buy() 2026-11-10'dan itibaren ATR verisi varsa GERCEK
                # bir stop_price/target_price yaziyor, yoksa hala NULL).
                elif "STOP" in sell_zone and pos["stop_price"] is not None and price <= pos["stop_price"]:
                    exit_reason = "stop"
                elif "TARGET" in sell_zone and pos["target_price"] is not None and price >= pos["target_price"]:
                    exit_reason = "target"
                elif pos["source"] == "auto" and now_str >= pos["max_hold_until"]:
                    exit_reason = "time_stop"
                elif row["score"] in sell_zone:
                    exit_reason = "signal_reversal" if pos["source"] == "auto" else "manual_sat_signal"

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
                    trade_events.append({
                        "kind": "sell",
                        "ticker": pos["ticker"],
                        "entry_price": pos["entry_price"],
                        "exit_price": price,
                        "exit_reason": exit_reason,
                        "pnl": pnl,
                        "timestamp": now_str,
                    })

            # 5-slot limiti artik TUM acik pozisyonlari sayar - kaynak
            # (auto/manual) farki YOK (2026-11-03'te degistirildi, bkz.
            # manual_buy()'daki AYNI degisiklik ve CLAUDE.md). Eskiden
            # SADECE source='auto' sayiliyordu ("bu ticker ZATEN acik mi"
            # kontrolu 2026-11-02'de source-notr yapilmisti ama SAYIM hala
            # auto'ya ozeldi) - bu, nakit manuel alimlarla tukenmisken
            # otomatik dongunun "ne kadar nakit varsa onunla gir" yedek
            # davranisiyla (allocation = min(per_slot_amount, cash)) 5.
            # auto slotu anlamsiz derecede kucuk bir tutarla (gercekten
            # yasandi: HUBVC, 2026-11-03, 2.404 TL - normalde ~200.000 TL
            # olmasi gerekirken) acmasina yol aciyordu, cunku sayim 4 auto +
            # 1 manuel = 5 TOPLAM acik pozisyonu "sadece 4 dolu slot" olarak
            # goruyordu. Artik kural: toplam 5 pozisyon aciksa (auto+manual
            # farketmez) YENI ALIM (auto da, manuel de) YAPILMAZ - "ne kadar
            # nakit varsa onunla gir" davranisi byle BIR DAHA tetiklenmez
            # (cunku tetiklenmeden once slot sayisi zaten dolmus olur).
            held_tickers = {p["ticker"] for p in open_positions} - closed_this_cycle
            open_count = len(held_tickers)

            # Pozisyon buyuklugu icin GUNCEL toplam deger (nakit + henuz
            # KAPANMAMIS acik pozisyonlarin o anki fiyatla degeri) hesaplanir -
            # sabit PAPER_STARTING_BALANCE DEGIL (2026-11-02'de kullanici
            # istegiyle degistirildi: "toplam bakiye ne kadarsa 5'e bolunsun,
            # ana para hic asilmasin" - bkz. CLAUDE.md). Nakit zaten yukarida
            # kapanan pozisyonlardan gelen satis geliriyle guncellendi;
            # positions_value SADECE hala acik olan (bu dongude kapanmamis)
            # pozisyonlari sayar, kapananlari TEKRAR saymamak icin.
            #
            # ⚠️ Rate-limit/veri-eksikligi duzeltmesi (2026-11-04'te eklendi):
            # bir pozisyonun bu dongude GECERLI bir fiyati gelmediyse (Yahoo
            # rate-limit'teyken veya baska bir hata yuzunden row hic yoksa/NaN
            # ise) eskiden bu pozisyon degere SIFIR katkida bulunuyordu -
            # sanki pozisyon birden degersiz olmus gibi, bu da equity'nin
            # (ve dolayisiyla Toplam K/Z'nin) o pozisyonun TUM yatirimi kadar
            # yanlislikla eksiye dusmesine yol aciyordu (kullanici bunu
            # "veri olmadigi icin kar-zarar eksi deger olarak raporlaniyor"
            # diye bildirdi). Duzeltme: fiyati gelmeyen bir pozisyon icin
            # KENDI allocation'i (= o pozisyon icin K/Z degisimi SIFIR kabul
            # edilir) kullanilir - deger ne cokertilir ne de yanlis bir kayip
            # uretilir, sadece "bu dongude bilgi yok" durumu NOTR karsilanir.
            # price_data_issue bayragi bu durumun olup olmadigini isaretler -
            # asagida (DB kilidi/commit'inden SONRA) Telegram'a TEK bir uyari
            # gonderilmesi icin kullanilir.
            price_data_issue = False
            still_open_value = 0.0
            for p in open_positions:
                if p["ticker"] in closed_this_cycle:
                    continue
                r = rows_by_ticker.get(p["ticker"])
                if r is not None and r["price"] == r["price"]:  # NaN degilse
                    still_open_value += r["price"] * p["shares"]
                else:
                    price_data_issue = True
                    still_open_value += p["allocation"]
            current_equity = cash + still_open_value
            per_slot_amount = current_equity / max_positions

            # YENI GUCLU_AL/MUTLAK_AL gecislerini tespit et - ikisi TEK bir
            # "guclu alim bolgesi" sayilir (frontend'deki markerZone() ile
            # AYNI mantik): buy_zone'un (kullanicinin sectigi etiket kumesi,
            # ESKIDEN sabit GUCLU_AL+MUTLAK_AL idi, 2026-11-06'da elle
            # degistirilebilir oldu) KENDI ICINDE (bolge degismeden) gidip
            # gelmek YENI alim tetiklemez, sadece bolgenin DISINDAN icine
            # girmek tetikler. "hic gorulmemis" (prev=None) BILINCLI olarak
            # "bolge disi" sayilir (prev is not None kontrolu KALDIRILDI,
            # 2026-11-02) - restart korumasi artik _seed_paper_last_score()'un
            # last_known_score'dan yaptigi seed ile saglaniyor, bu yuzden
            # prev=None SADECE gercekten YENI (ilk kez dinamik BIST 300
            # havuzuna giren) bir hisse icin olur ve bu da GECERLI bir alim
            # adayi sayilmali - eskiden boyle bir hisse ZATEN GUCLU_AL/
            # MUTLAK_AL olarak havuza girdiginde SONSUZA KADAR atlaniyordu
            # (bkz. CLAUDE.md). rows zaten skor gucu + gunluk % sirali (bkz.
            # web_bist.py: _scan_once()'taki _sort_key), bu yuzden
            # candidate_tickers de dogal olarak o oncelik sirasinda.
            candidates = []
            for row in rows:
                ticker = row["ticker"]
                prev = _paper_last_score.get(ticker)
                if prev not in buy_zone and row["score"] in buy_zone:
                    candidates.append(row)
                _paper_last_score[ticker] = row["score"]

            free_slots = max_positions - open_count
            for row in candidates:
                if free_slots <= 0:
                    break
                ticker = row["ticker"]
                if ticker in held_tickers or ticker in closed_this_cycle:
                    continue  # bu hisse ZATEN acik (kaynagi auto ya da manual farketmez) - tekrar alinmaz
                if row.get("ticker_flag") == "SIKINTILI":
                    continue  # kullanici bu hisseyi elle "Sorunlu" isaretlemis - GUCLU_AL/MUTLAK_AL
                    # gelse bile otomatik alim YAPILMAZ (2026-11-09'da eklendi, bkz. CLAUDE.md
                    # "Hisse isaretleri" - kullanicinin acik istegi). Manuel alim bundan ETKILENMEZ -
                    # kullanici isterse "Sorunlu" isaretli bir hisseyi yine de elle alabilir.
                if row.get("swing_stop") is None or row.get("swing_target") is None:
                    continue  # ATR verisi yetersiz - guvenli bir stop/hedef yok
                vol20 = row.get("vol20")
                if vol20 is None or vol20 != vol20 or vol20 < PAPER_MIN_VOL20:
                    continue  # cok sakin/durgun hisse - backtest'te GUCLU_AL burada edge tasimiyordu
                price = row["price"]
                if price != price or price <= 0:
                    continue  # NaN/gecersiz fiyat (data glitch) - bu dongu atlanir

                allocation = min(per_slot_amount, cash)
                if allocation <= 0:
                    break
                shares = allocation / price
                max_hold_until = (now + timedelta(days=PAPER_MAX_HOLD_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
                conn.execute(
                    """INSERT INTO paper_trades
                           (ticker, status, entry_time, entry_price, shares, allocation,
                            stop_price, target_price, max_hold_until, source)
                       VALUES (?, 'open', ?, ?, ?, ?, ?, ?, ?, 'auto')""",
                    (ticker, now_str, price, shares, allocation, row["swing_stop"], row["swing_target"], max_hold_until),
                )
                cash -= allocation
                held_tickers.add(ticker)
                free_slots -= 1
                trade_events.append({
                    "kind": "buy",
                    "ticker": ticker,
                    "price": price,
                    "shares": shares,
                    "allocation": allocation,
                    "timestamp": now_str,
                })

            conn.execute("UPDATE paper_account SET cash = ? WHERE id = 1", (cash,))

            # Ayni rate-limit duzeltmesi (yukaridaki still_open_value ile AYNI
            # mantik/gerekce) - fiyati bu dongude gelmeyen bir pozisyon
            # SIFIR degil, kendi allocation'i kadar sayilir (K/Z degisimi
            # NOTR), boylece paper_equity_history'ye (ve dolayisiyla
            # get_paper_summary()'nin equity/total_pnl/unrealized_pnl'ine)
            # yanlislikla cokmus bir deger YAZILMAZ.
            positions_value = 0.0
            for r in conn.execute("SELECT ticker, shares, allocation FROM paper_trades WHERE status = 'open'").fetchall():
                row = rows_by_ticker.get(r["ticker"])
                if row is not None and row["price"] == row["price"]:  # NaN degilse
                    positions_value += row["price"] * r["shares"]
                else:
                    price_data_issue = True
                    positions_value += r["allocation"]
            conn.execute(
                "INSERT INTO paper_equity_history (timestamp, cash, positions_value, equity) VALUES (?, ?, ?, ?)",
                (now_str, cash, positions_value, cash + positions_value),
            )

            conn.commit()
        finally:
            conn.close()

    for event in trade_events:
        _send_paper_trade_notification(event)

    # Rate-limit/veri-eksikligi uyarisi - DB kilidi/commit'inden SONRA (ag
    # istegi kilidi gereksiz uzun tutmasin diye, _send_paper_trade_notification
    # ile AYNI desen). Sorun SURERKEN (her dongude, 5dk'da bir) tekrar tekrar
    # SPAM edilmez - TEK seferlik uyari, sorun duzelince (bir sonraki temiz
    # dongude) bayrak sifirlanir ki ileride tekrar tetiklenebilsin.
    global _paper_price_issue_alert_sent
    if price_data_issue:
        if not _paper_price_issue_alert_sent:
            send_health_alert(
                "Sanal Al-Sat: bir veya daha fazla acik pozisyonun fiyati bu "
                "dongude alinamadi (rate limit veya baska bir veri sorunu "
                "olabilir). Etkilenen pozisyonlarin K/Z'si bu dongude NOTR "
                "(degisim yok) kabul edildi, yanlis bir kayip raporlanmadi."
            )
            _paper_price_issue_alert_sent = True
    else:
        _paper_price_issue_alert_sent = False


PAPER_MAX_POSITIONS_MIN = 1
PAPER_MAX_POSITIONS_MAX = 50


def get_paper_max_positions() -> int:
    """Kullanicinin elle ayarladigi maksimum acik pozisyon slotunu DB'den
    okur (2026-11-06'da eklendi, bkz. CLAUDE.md) - eskiden sabit
    PAPER_MAX_POSITIONS=5 idi, artik Sanal Al-Sat panelinden degistirilebilir.
    run_paper_trading_cycle()/manual_buy() her cagrida bunu tazeler (tek
    satirlik sorgu, pahali degil) - deger degisse de ayri bir restart/cache
    temizligi GEREKMEZ."""
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT max_positions FROM paper_account WHERE id = 1").fetchone()
        return row["max_positions"] if row else PAPER_MAX_POSITIONS
    finally:
        conn.close()


def set_paper_max_positions(n: int) -> None:
    """Maksimum acik pozisyon slotunu elle degistirir - 1..50 araliginda
    (cagiran taraf/API bu araligi ayrica dogrulamali, burada da guvenlik
    icin tekrar sinirlanir). Mevcut ACIK pozisyonlara DOKUNULMAZ - slot
    sayisi dusurulurse (ornegin 5'ten 3'e) zaten acik olan pozisyonlar
    ZORLA KAPATILMAZ, sadece yeni alim/otomatik alim bu yeni sinira gore
    calisir (open_count >= yeni_max oldugunda dogal olarak durur)."""
    n = max(PAPER_MAX_POSITIONS_MIN, min(PAPER_MAX_POSITIONS_MAX, int(n)))
    with _DB_LOCK:
        conn = get_db_connection()
        try:
            conn.execute("UPDATE paper_account SET max_positions = ? WHERE id = 1", (n,))
            conn.commit()
        finally:
            conn.close()


def set_paper_cash(amount: float) -> None:
    """Sanal nakti elle istenen tutara ayarlar - kullanici Sanal Al-Sat
    panelinden 'Nakit' degerini duzenleyebiliyor. Negatif deger cagiran
    tarafta (API) reddedilmeli, burada sadece yazilir."""
    with _DB_LOCK:
        conn = get_db_connection()
        try:
            conn.execute("UPDATE paper_account SET cash = ? WHERE id = 1", (amount,))
            conn.commit()
        finally:
            conn.close()


def reset_paper_trading(starting_balance: float) -> None:
    """Sanal al-sat'i sifirlar: ACIK islemler, equity gecmisi ve manuel
    takip listesi SILINIR. Nakit VE starting_balance (toplam K/Z'nin
    olculdugu baz) verilen starting_balance'a esitlenir - sabit
    PAPER_STARTING_BALANCE kullanilmaz, kullanici her sifirlamada kendi
    baslangic tutarini seciyor. Frontend'de bu, "emin misin" onayi
    SONRASINDA cagrilir. _paper_last_score (hangi ticker'in GUCLU_AL'a
    yeni gectigini takip eden hafiza) KASITLI olarak DOKUNULMAZ - yoksa
    sifirlama hemen ardindan, zaten GUCLU_AL'da olan tum hisseleri 'yeni
    gecis' sayip ayni restart-hatasini (bkz. CLAUDE.md) tekrar tetikler.

    ⚠️ KAPANAN (status='closed') islemler ARTIK SILINMIYOR (2026-11-05'te
    kullanici istegiyle degistirildi - "nakit sifirlanirken kapanan
    islemler hicbir zaman sifirlanmasin") - gecmis performans/aciklama
    notlari (bkz. note sutunu) kalici bir kayit olarak korunuyor, sadece
    ACIK pozisyonlar (henuz sonuclanmamis, yeni baslangic sermayesiyle
    artik anlamsiz kalan) temizleniyor."""
    with _DB_LOCK:
        conn = get_db_connection()
        try:
            conn.execute("DELETE FROM paper_trades WHERE status = 'open'")
            conn.execute("DELETE FROM paper_equity_history")
            conn.execute("DELETE FROM paper_watchlist")
            conn.execute(
                "UPDATE paper_account SET cash = ?, starting_balance = ? WHERE id = 1",
                (starting_balance, starting_balance),
            )
            conn.commit()
        finally:
            conn.close()


def get_paper_watchlist():
    """Kullanicinin elle ekledigi, otomatik sistemden BAGIMSIZ manuel takip
    listesini dondurur - web_bist.py: _scan_once() bu ticker'lari da
    (acik pozisyonlar gibi) tarama havuzuna ekler, boylece fiyatsiz kalmaz."""
    conn = get_db_connection()
    try:
        cur = conn.execute("SELECT ticker, added_at FROM paper_watchlist ORDER BY added_at DESC")
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def add_to_paper_watchlist(ticker: str) -> None:
    with _DB_LOCK:
        conn = get_db_connection()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO paper_watchlist (ticker, added_at) VALUES (?, ?)",
                (ticker, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            conn.commit()
        finally:
            conn.close()


def remove_from_paper_watchlist(ticker: str) -> None:
    """Takip listesinden cikarma, o ticker'daki ACIK bir manuel pozisyona
    DOKUNMAZ - sadece 'hizli alim listesi'nden kaldirir, elindeki pozisyon
    Acik Pozisyonlar'da gorunmeye devam eder."""
    with _DB_LOCK:
        conn = get_db_connection()
        try:
            conn.execute("DELETE FROM paper_watchlist WHERE ticker = ?", (ticker,))
            conn.commit()
        finally:
            conn.close()


def manual_buy(ticker: str, allocation: float, price: float, swing_stop=None, swing_target=None):
    """Kullanicinin elle, istedigi an, istedigi tutarla satin almasi.
    Basarisizsa (tutar<=0, fiyat gecersiz, nakit yetersiz, bu ticker'da
    ZATEN acik bir pozisyon varsa - kaynagi auto ya da manual farketmez,
    2026-11-02'de eklendi - VEYA toplam acik pozisyon sayisi (kaynak
    farksiz) PAPER_MAX_POSITIONS'a ULASMISSA - 2026-11-03'te eklendi, bkz.
    asagidaki yorum) None doner.

    swing_stop/swing_target (2026-11-10'da eklendi, kullanici istegi: "manuel
    acik pozisyonlarda hedef al sat neden yok" -> "otomatik hesapla (auto ile
    ayni mantik)") - auto pozisyonlarin ayni alanlarla (bkz. run_paper_trading_
    cycle()'daki candidate dongusu) BESLENDIGI AYNI ATR tabanli swing stop/
    hedef degerleri, cagiran (web_bist.py) tarafindan _state["rows"]'tan
    okunup buraya gecirilir - bu fonksiyon Yahoo'ya kendisi HIC ISTEK ATMAZ.
    None/NaN gecilirse (ATR verisi yetersiz - yeni listelenen bir hisse ya da
    veri sorunu) stop_price/target_price eskisi gibi NULL yazilir, alim
    REDDEDILMEZ - kullanici BILEREK elle aliyor, auto'nun aksine (orada
    swing_stop/target yoksa aday TAMAMEN atlanir) burada bu EKSIKLIK alimi
    bloke etmemeli."""
    if allocation <= 0 or price != price or price <= 0:
        return None
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _DB_LOCK:
        conn = get_db_connection()
        try:
            account_row = conn.execute("SELECT cash, max_positions FROM paper_account WHERE id = 1").fetchone()
            cash = account_row["cash"]
            if allocation > cash:
                return None
            already_open = conn.execute(
                "SELECT 1 FROM paper_trades WHERE ticker = ? AND status = 'open' LIMIT 1", (ticker,)
            ).fetchone()
            if already_open is not None:
                return None
            # Slot limiti artik kaynak farki OLMADAN TUM acik pozisyonlari
            # sayar (2026-11-03'te degistirildi - eskiden SADECE source='auto'
            # sayiliyordu, manuel alimlar limite HIC dahil degildi, bkz.
            # run_paper_trading_cycle()'daki AYNI degisiklik). Kok sebep:
            # eski kurguda nakit, auto dongunun "kalan nakit kadar al"
            # yedek davranisiyla (allocation = min(per_slot_amount, cash))
            # tukeniyor, 5. auto slot anlamsiz derecede kucuk bir tutarla
            # (ornegin ~2.400 TL, normalde ~200.000 TL olmasi gerekirken)
            # aciliyordu - cunku o anda zaten 4 auto + birden fazla manuel
            # pozisyon nakti bitirmisti ama sayim SADECE auto'ya bakiyordu.
            # Simdi hem bu fonksiyon hem run_paper_trading_cycle() "toplam
            # max_positions kadar pozisyon aciksa (auto+manual farketmez)
            # HICBIR yeni alim yapilmaz" kuralini PAYLASIYOR. max_positions
            # artik sabit 5 DEGIL, kullanici tarafindan degistirilebilen bir
            # DB degeri (bkz. set_paper_max_positions(), 2026-11-06).
            open_count = conn.execute(
                "SELECT COUNT(*) AS n FROM paper_trades WHERE status = 'open'"
            ).fetchone()["n"]
            if open_count >= account_row["max_positions"]:
                return None
            shares = allocation / price
            # Manuel pozisyonlar da artik (2026-11-10'dan itibaren, kullanici
            # istegiyle - bkz. yukaridaki docstring) auto ile AYNI ATR tabanli
            # stop_price/target_price'i aliyor - run_paper_trading_cycle()'daki
            # cikis kontrolu BUNLARI zaten source'tan BAGIMSIZ okuyordu (sadece
            # "is not None" sartiyla), bu yuzden o fonksiyonda HICBIR degisiklik
            # GEREKMEDI - sadece buraya GERCEK deger yazilmasi yeterli. Deger
            # yoksa (ATR verisi yetersiz) NULL kalir, alim REDDEDILMEZ.
            if swing_stop is not None and swing_stop == swing_stop:
                stop_price = swing_stop
            else:
                stop_price = None
            if swing_target is not None and swing_target == swing_target:
                target_price = swing_target
            else:
                target_price = None
            # max_hold_until manuel pozisyonlar icin hala SADECE bilgi amacli
            # (time_stop kontrolu run_paper_trading_cycle()'da acikca
            # source=='auto' sartina bagli, bu DEGISMEDI - kullanici sadece
            # stop/hedefi geri istedi, sure asimini degil).
            max_hold_until = (datetime.now() + timedelta(days=PAPER_MAX_HOLD_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
            cur = conn.execute(
                """INSERT INTO paper_trades
                       (ticker, status, entry_time, entry_price, shares, allocation,
                        stop_price, target_price, max_hold_until, source)
                   VALUES (?, 'open', ?, ?, ?, ?, ?, ?, ?, 'manual')""",
                (ticker, now_str, price, shares, allocation, stop_price, target_price, max_hold_until),
            )
            conn.execute("UPDATE paper_account SET cash = ? WHERE id = 1", (cash - allocation,))
            conn.commit()
            return {"id": cur.lastrowid, "ticker": ticker, "entry_price": price, "shares": shares}
        finally:
            conn.close()


def manual_sell(trade_id: int, price: float):
    """Kullanicinin elle, istedigi an satmasi - status='open' olan HERHANGI
    bir pozisyon icin calisir, kaynagi (auto/manual) FARKETMEZ (2026-11-02'de
    genisletildi - eskiden sadece source='manual' icin calisiyordu, kullanici
    "otomatik ve manuel pozisyonlarda da Sat butonu aktif olsun" istedi).
    Basarisizsa (satir yok/zaten kapali) None doner."""
    if price != price or price <= 0:
        return None
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _DB_LOCK:
        conn = get_db_connection()
        try:
            pos = conn.execute(
                "SELECT * FROM paper_trades WHERE id = ? AND status = 'open'",
                (trade_id,),
            ).fetchone()
            if pos is None:
                return None
            pnl = (price - pos["entry_price"]) * pos["shares"]
            conn.execute(
                """UPDATE paper_trades
                       SET status='closed', exit_time=?, exit_price=?, exit_reason='manual', pnl=?
                   WHERE id=?""",
                (now_str, price, pnl, trade_id),
            )
            cash = conn.execute("SELECT cash FROM paper_account WHERE id = 1").fetchone()["cash"]
            conn.execute("UPDATE paper_account SET cash = ? WHERE id = 1", (cash + price * pos["shares"],))
            conn.commit()
            return {"id": trade_id, "pnl": pnl}
        finally:
            conn.close()


def get_paper_positions():
    """Su an acik olan sanal pozisyonlari dondurur (giris bilgileriyle -
    guncel fiyat/K-Z hesaplamasi web_bist.py'de canli rows ile
    birlestirilerek yapilir)."""
    conn = get_db_connection()
    try:
        cur = conn.execute("SELECT * FROM paper_trades WHERE status = 'open' ORDER BY entry_time DESC")
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_paper_trades(limit: int = 2000):
    """Kapanmis sanal islemleri (en yeni once) dondurur. Varsayilan limit
    200'den 2000'e cikarildi (2026-11-05) - reset_paper_trading() artik
    kapanan islemleri SILMEDIGI icin (bkz. orada) bu liste zamanla
    biriken, buyuyen bir gecmis oluyor; frontend'deki pager (hisse
    tablosuyla AYNI desen - bkz. CLAUDE.md) TUM satirlari tek seferde
    cekip kendi icinde sayfaliyor, bu yuzden backend'in cok erken
    kesmemesi gerekiyor."""
    conn = get_db_connection()
    try:
        cur = conn.execute(
            "SELECT * FROM paper_trades WHERE status = 'closed' ORDER BY exit_time DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


PAPER_TRADE_NOTE_MAX_LEN = 250


def set_paper_trade_note(trade_id: int, note: str) -> bool:
    """Kapanan bir sanal islemin aciklama notunu gunceller (2026-11-05'te
    eklendi, kullanici istegi: "kapanan islemlerde aciklama kismi olsun,
    250 karakteri asmayacak sekilde istenilen her sey yazilabilsin").
    SADECE status='closed' olan bir satiri gunceller - acik pozisyonlar
    icin bu endpoint kullanilmaz (henuz sonuclanmamis bir islemin "notu"
    kavrami yok, kullanici isteginin kapsami zaten SADECE kapanan
    islemlerdi). Boş/None not SILMEK icin kullanilir (notu kaldirmak
    isteyen kullanici input'u bosaltabilir). Uzunluk kontrolu burada DA
    yapilir (cagiran taraf - API route - zaten kontrol etse de, bu
    fonksiyon DOGRUDAN cagrilsa bile guvenli kalsin diye). Eslesen satir
    yoksa (yanlis id ya da hala acik) False doner, cagiran taraf 404/400
    donebilsin diye."""
    if note is not None and len(note) > PAPER_TRADE_NOTE_MAX_LEN:
        raise ValueError(f"Aciklama en fazla {PAPER_TRADE_NOTE_MAX_LEN} karakter olabilir.")
    with _DB_LOCK:
        conn = get_db_connection()
        try:
            cur = conn.execute(
                "UPDATE paper_trades SET note = ? WHERE id = ? AND status = 'closed'",
                (note or None, trade_id),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def get_paper_equity_history(since_timestamp: str):
    """since_timestamp'ten itibaren equity (nakit + acik pozisyon degeri)
    gecmisini dondurur - Skor Gecmisi'ndeki gunluk/saatlik/5dk sekmeleriyle
    ayni sekilde (ham veri, agregasyon okuma anında) grafik/tablo icin.
    (2026-11-03'teki olu kod taramasinda okuma tarafi - bu fonksiyon + ona
    bagli /api/paper/equity route'u - hic kullanilmadigi icin kaldirilmisti;
    2026-11-04'te kullanicinin "projeye ne eklenebilir" sorusuna yanit olarak
    Sanal Al-Sat equity egrisi grafigi istenince GERI EKLENDI, bkz. CLAUDE.md.
    Yazma tarafi - run_paper_trading_cycle()'in paper_equity_history'ye
    INSERT'i - HIC KALDIRILMAMISTI, veri zaten tum bu sure boyunca
    birikiyordu.)"""
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


def _compute_paper_performance_metrics(conn, closed_pnls):
    """get_paper_summary()'nin ACIK connection'ini paylasir (ekstra baglanti
    acmaz). Sharpe orani/max drawdown TUM equity gecmisinden (reset'lerle
    equity_history silinse de - bkz. reset_paper_trading - bu fonksiyon o
    anda ne kadar veri varsa onu kullanir, ozel bir pencere/cutoff YOK, ayni
    get_paper_summary'nin kendisi gibi "simdiye kadarki tum surec" sorusuna
    cevap veriyor). Kapanan islem sayisi azsa (ornegin yeni sifirlama sonrasi)
    None'lar donuyor - 0'a bolme/istatistiksel anlamsizlik riski olmadan."""
    equity_rows = conn.execute(
        "SELECT timestamp, equity FROM paper_equity_history ORDER BY id ASC"
    ).fetchall()

    # Maksimum drawdown: simdiye kadarki en yuksek tepe noktasindan sonraki
    # en buyuk dusus (tepe her yeni zirveyle guncellenir, dusus o anki
    # tepeye gore olculur - standart "peak-to-trough" tanimi).
    max_drawdown = 0.0
    max_drawdown_pct = 0.0
    peak = None
    for r in equity_rows:
        eq = r["equity"]
        if eq is None:
            continue
        if peak is None or eq > peak:
            peak = eq
        if peak:
            dd = peak - eq
            if dd > max_drawdown:
                max_drawdown = dd
                max_drawdown_pct = (dd / peak * 100.0) if peak else 0.0

    # Sharpe orani: GUNLUK equity degisiminden (her gunun SON kaydi - ayni
    # gun icindeki tarama-dongusu dalgalanmalari gurultu sayilip atlanir),
    # yillik 252 islem gunu varsayimiyla annualize edilir. En az 3 farkli
    # gun (2 getiri) olmadan anlamli bir std sapma hesaplanamaz - None doner.
    daily_last_equity = {}
    for r in equity_rows:
        if r["equity"] is None:
            continue
        daily_last_equity[r["timestamp"][:10]] = r["equity"]
    daily_values = [daily_last_equity[d] for d in sorted(daily_last_equity.keys())]
    daily_returns = []
    for i in range(1, len(daily_values)):
        prev = daily_values[i - 1]
        if prev:
            daily_returns.append((daily_values[i] - prev) / prev)
    sharpe_ratio = None
    if len(daily_returns) >= 2:
        mean_r = sum(daily_returns) / len(daily_returns)
        variance = sum((x - mean_r) ** 2 for x in daily_returns) / (len(daily_returns) - 1)
        std_r = variance ** 0.5
        if std_r > 0:
            sharpe_ratio = (mean_r / std_r) * (252 ** 0.5)

    wins = [p for p in closed_pnls if p > 0]
    losses = [p for p in closed_pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None

    return {
        "max_drawdown": max_drawdown,
        "max_drawdown_pct": max_drawdown_pct,
        "sharpe_ratio": sharpe_ratio,
        "profit_factor": profit_factor,
        "avg_win": (sum(wins) / len(wins)) if wins else None,
        "avg_loss": (sum(losses) / len(losses)) if losses else None,
        "best_trade": max(closed_pnls) if closed_pnls else None,
        "worst_trade": min(closed_pnls) if closed_pnls else None,
    }


_paper_benchmark_cache = {}  # "data" -> (fetched_at, since_date, pct)


def get_paper_benchmark_return(since_timestamp):
    """paper_equity_history'nin EN ESKI (ilk) satirinin tarihinden (since_timestamp,
    "YYYY-MM-DD HH:MM:SS") BUGUNE kadar BENCHMARK_TICKER'in (XU100.IS, RS
    karsilastirmasinda da kullanilan AYNI BIST100 endeksi) toplam yuzde
    degisimini dondurur - Sanal Al-Sat performansini "sadece al-ve-tut
    yapsaydim ne olurdu" ile kiyaslamak icin (2026-11-07'de eklendi, bkz.
    CLAUDE.md "BIST100 benchmark karsilastirmasi"). since_timestamp None ise
    (hic equity kaydi yoksa - orn. taze/sifirlanmis hesap) karsilastirilacak
    bir donem olmadigi icin None doner.
    `_benchmark_cache`/`get_benchmark_daily_pct()` SADECE bugunku gunluk %
    degisimi onbelleklerken, bu fonksiyon BASLANGIC gununden BUGUNE kadarki
    TOPLAM degisimi tutar - ayri bir onbellek GEREKTI. `_chart_cache`/
    `/api/history`'nin lazy-fetch-with-TTL deseniyle AYNI felsefe: Flask
    route'undan (get_paper_summary() uzerinden) DOGRUDAN, senkron cagrilir
    (arka plan tarama donguusune TASINMADI - /api/indicators de zaten
    ayni sekilde route icinde canli Yahoo cekiyor) ama TTL = BIST_REFRESH_
    SECONDS-5 oldugu icin ayni 5 dakikalik pencerede gelen TUM istekler
    (kac sekme/kullanici olursa olsun) PAYLASILAN tek bir Yahoo cagrisini
    kullanir."""
    if not since_timestamp:
        return None
    since_date = since_timestamp[:10]
    cached = _paper_benchmark_cache.get("data")
    if cached and cached[1] == since_date and time.time() - cached[0] < BIST_REFRESH_SECONDS - 5:
        return cached[2]
    pct = None
    try:
        tk = yf.Ticker(BENCHMARK_TICKER)
        hist = tk.history(start=since_date, interval="1d", auto_adjust=False)
        if len(hist) >= 1:
            start_price = float(hist["Close"].iloc[0])
            current_price = float(hist["Close"].iloc[-1])
            pct = pct_change(current_price, start_price)
            if pct != pct:  # NaN - pct_change gecersiz girdide NaN dondurur, JSON'da temsil edilemez
                pct = None
    except Exception:
        pct = None
    _paper_benchmark_cache["data"] = (time.time(), since_date, pct)
    return pct


def get_paper_summary():
    """Sanal hesabin o anki ozetini dondurur: nakit, acik pozisyon degeri,
    toplam equity, gerceklesmis/gerceklesmemis K-Z, kazanma orani. Acik
    pozisyonlarin guncel degeri icin en son equity satirini kullanir (
    run_paper_trading_cycle her dongude bunu zaten gunceller).
    2026-11-07'de Sharpe orani/max drawdown/profit factor/ort. kazanc-kayip/
    en iyi-en kotu islem (performans metrikleri) eklendi - bkz. CLAUDE.md
    "Equity performans metrikleri"."""
    conn = get_db_connection()
    try:
        account = conn.execute(
            "SELECT cash, starting_balance, max_positions FROM paper_account WHERE id = 1"
        ).fetchone()
        last_equity = conn.execute(
            "SELECT equity, positions_value FROM paper_equity_history ORDER BY id DESC LIMIT 1"
        ).fetchone()
        closed = conn.execute("SELECT pnl FROM paper_trades WHERE status = 'closed'").fetchall()
        open_count = conn.execute("SELECT COUNT(*) AS c FROM paper_trades WHERE status = 'open'").fetchone()["c"]

        closed_pnls = [r["pnl"] for r in closed if r["pnl"] is not None]
        realized_pnl = sum(closed_pnls)
        equity = last_equity["equity"] if last_equity else account["cash"]
        positions_value = last_equity["positions_value"] if last_equity else 0.0
        wins = sum(1 for p in closed_pnls if p > 0)

        open_allocation = conn.execute(
            "SELECT COALESCE(SUM(allocation), 0) AS total FROM paper_trades WHERE status = 'open'"
        ).fetchone()["total"]
        unrealized_pnl = positions_value - open_allocation

        summary = {
            "cash": account["cash"],
            "positions_value": positions_value,
            "equity": equity,
            "starting_balance": account["starting_balance"],
            "total_pnl": equity - account["starting_balance"],
            "realized_pnl": realized_pnl,
            "unrealized_pnl": unrealized_pnl,
            "open_positions": open_count,
            "closed_trades": len(closed_pnls),
            "win_rate": (wins / len(closed_pnls) * 100.0) if closed_pnls else None,
            "max_positions": account["max_positions"],
        }
        summary.update(_compute_paper_performance_metrics(conn, closed_pnls))

        # BIST100 benchmark karsilastirmasi (2026-11-07'de eklendi) - hesabin
        # TAKIP EDILDIGI tum sure boyunca (equity_history'nin en eski satiri)
        # "sadece BIST100'u al-ve-tut yapsaydim ne olurdu" sorusunun cevabi +
        # alfa (hesabin kendi getirisi EKSI bu getiri). starting_balance 0/None
        # olamaz (reset_paper_trading() her zaman pozitif bir deger yazar) ama
        # savunmacı kontrol yine de eklendi.
        first_equity = conn.execute(
            "SELECT timestamp FROM paper_equity_history ORDER BY id ASC LIMIT 1"
        ).fetchone()
        benchmark_return_pct = get_paper_benchmark_return(first_equity["timestamp"] if first_equity else None)
        account_return_pct = (
            (equity - account["starting_balance"]) / account["starting_balance"] * 100.0
            if account["starting_balance"] else None
        )
        summary["benchmark_return_pct"] = benchmark_return_pct
        summary["alpha_pct"] = (
            account_return_pct - benchmark_return_pct
            if (account_return_pct is not None and benchmark_return_pct is not None)
            else None
        )
        return summary
    finally:
        conn.close()

