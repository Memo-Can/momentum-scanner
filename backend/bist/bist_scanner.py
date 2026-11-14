#!/usr/bin/env python3
"""BIST is katmani - ORTAK ALTYAPI (DB semasi/baglantisi) + skor/sinyal
gecmisi + hisse isaretleri. Bu modulun kapsami 2026-11-14'te (bkz. CLAUDE.md
"Indicators.py ayristirildi" ve devami - bist_indicators.py/notification.py/
bist_market_data.py/bist_paper_trading.py bolunmeleri) ONEMLI OLCUDE DARALDI: eskiden
TUM is katmanini (Yahoo veri cekme, indikator matematigi, Sanal Al-Sat)
icinde barindiriyordu, artik SADECE asagidakileri icerir:

  1) **DB SEMASI/baglantisi** (`get_db_connection()`/`_DB_LOCK`/`_init_db()`/
     `_init_paper_trading()`) - TUM tablolari (score_history/signal_log/
     last_known_score/score_since/paper_account/paper_trades/
     paper_equity_history/paper_watchlist/ticker_flags) burada olusturur,
     SADECE paper-trading'e ozel bir sema parcasi degil. `settings.py`/
     `admin.py`/`bist_market_data.py`/`bist_paper_trading.py` bu dosyayi get_db_
     connection()/_DB_LOCK VE (settings.py/bist_paper_trading.py icin) birkac
     paylasilan sabit (`PAPER_STARTING_BALANCE`/`PAPER_MAX_POSITIONS`/
     `_STRONG_BUY_ZONE`/`_DEFAULT_SELL_ZONE`/`_DEFAULT_NOTIFY_ZONE`) icin
     TEK YONLU import eder - bu dosya ONLARI (ya da bist_indicators.py/bist_market_data.py/
     bist_paper_trading.py'yi) HIC import ETMEZ, tersi DAIRESEL IMPORT olurdu.
  2) **Skor/sinyal gecmisi** (`log_score_history`/`log_signal_changes`/
     `get_score_history`/`_send_push_notifications` + `_seed_last_logged_
     scores`/`_seed_score_since`/`backfill_score_since`) - her tarama
     dongusunde TUM hisseler icin skor kaydi + GUCLU_AL/SAT gecislerinin
     Telegram bildirimi (notification.py uzerinden).
  3) **Hisse isaretleri** (`get_all_ticker_flags`/`set_ticker_flag`/
     `_seed_ticker_flags`) - kullanicinin "Guvenilir"/"Sorunlu" olarak elle
     isaretledigi hisseler (bist_paper_trading.py'nin otomatik alim/satim
     kararini etkiler, ama veri BURADA tutulur).
  4) **Piyasa takvimi** (`is_market_open`) - BIST'in hafta ici/saat
     penceresi disinda tarama dongusunun Yahoo'ya istek atmamasi icin.

Bu modul dogrudan calistirilmaz - bist_terminal.py/bist_web.py bu modulu
`import scanner_bist as scanner` ile kullanir; Yahoo'dan veri cekme/indikator
hesaplama/Sanal Al-Sat mantigi icin `bist_market_data.py`/`bist_indicators.py`/
`bist_paper_trading.py`'yi AYRICA import ederler."""

import json
import os
import sqlite3
import sys
import threading
from datetime import datetime

# backend/bist/ icindeki dosyalar backend/'de KALAN modulleri (notification.py)
# bulabilsin diye ust dizin sys.path'e eklenir (2026-11-14'te eklendi, bkz.
# CLAUDE.md "Dosyalar bist/ klasorune tasindi") - tests/test_scanner.py'nin
# ZATEN kullandigi AYNI sys.path.insert deseni, SADECE yon TERS (burada
# ust dizine, orada alt dizine).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import notification  # noqa: E402

# Calisma zamanlamasi (BIST_REFRESH_SECONDS, BIST acik saatleri) ARTIK
# burada TANIMLANMIYOR - app_settings.json'dan OKUNUP bu modulun isim
# alaninda sabit olarak disa veriliyor (scanner.BIST_REFRESH_SECONDS vb.) -
# .py yerine .json: duz veri, kod DEGIL, "yazilim standartlarina" uygun
# ayri bir ayar dosyasi. Anahtarlar BIST_ onekli - ileride baska bir
# piyasanin (orn. ABD/kripto) ayni dosyaya/benzer bir dosyaya kendi
# REFRESH_SECONDS/MARKET_OPEN_* degerleri eklenirse BIST'inkiyle
# KARISMASIN diye. app_settings.json backend/'de KALDI (bist/'e tasinmadi) -
# bu dosya artik backend/bist/'te oldugu icin yol bir ust dizine cikiyor.
_APP_SETTINGS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app_settings.json")
with open(_APP_SETTINGS_PATH, "r", encoding="utf-8") as _f:
    _app_settings = json.load(_f)

BIST_REFRESH_SECONDS = _app_settings["BIST_REFRESH_SECONDS"]
BIST_MARKET_OPEN_HOUR = _app_settings["BIST_MARKET_OPEN_HOUR"]
BIST_MARKET_OPEN_MINUTE = _app_settings["BIST_MARKET_OPEN_MINUTE"]
BIST_MARKET_CLOSE_HOUR = _app_settings["BIST_MARKET_CLOSE_HOUR"]
BIST_MARKET_CLOSE_MINUTE = _app_settings["BIST_MARKET_CLOSE_MINUTE"]

def is_market_open(now=None) -> bool:
    """BIST hisse tarama dongusunun (bist_web.py: _scan_loop(), bkz.
    oradaki cagri) Yahoo'ya istek atmasi icin "piyasa acik" sayilan an mi -
    hafta ici (Pazartesi-Cuma) VE app_settings.json'daki BIST_MARKET_OPEN_*/
    BIST_MARKET_CLOSE_* penceresi icindeyse True. Market seridi (doviz/emtia/
    kripto, bist_web.py: _market_scan_loop()) buna BAKMAZ - o
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

# BIST30_TICKERS/BIST100_TICKERS/BIST_INDEX_LISTS_VERIFIED_ON/
# _check_index_lists_freshness() artik BURADA DEGIL, bist_market_data.py'ye
# tasindi (2026-11-14, bkz. CLAUDE.md "Indicators.py ayristirildi" ve
# devami) - BIST evreninin tanimi, Yahoo veri cekme katmaninin bir parcasi.

# GUCLU_AL/GUCLU_SAT sinyallerinin gecmisini ve her dongudeki skor gecmisini
# kaydeder - zamanla bu kayitlara bakip Skor'un gercekte ise yarayip
# yaramadigini (sinyalden sonra fiyat ne oldu) kendi verinizle
# degerlendirebilirsiniz. Eskiden iki ayri CSV cifti (canli + backfill) idi;
# 2026-10'da tek bir SQLite dosyasina (iki tabloya, source='live'/'backfill'
# sutunuyla ayristirilarak) tasindi - bkz. migrate_csv_to_sqlite.py. Sebep:
# canli score_history CSV'si milyonlarca satira cikinca (3M+/130MB+) her
# uygulama baslangicinda tamami belleğe okunuyordu (bkz. CLAUDE.md), SQLite
# indeksli sorgu sayesinde bu gerekmiyor.
# data/ de backend/'de KALDI (bist/'e tasinmadi) - ayni sebeple bir ust dizine cikiyor.
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
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
# PAPER_MAX_HOLD_DAYS/PAPER_MIN_VOL20 artik BURADA DEGIL, bist_paper_trading.py'ye
# tasindi (2026-11-14, bkz. CLAUDE.md) - SADECE orada (run_paper_trading_cycle)
# kullanilan strateji sabitleri, DB semasina ihtiyaclari yok.
# Asagidaki 3 sabit SADECE paper_account.buy_criteria/sell_criteria/
# notify_criteria sutunlarinin CREATE/ALTER TABLE varsayilan degeri icin var -
# gecerli-deger kumeleri (PAPER_*_CRITERIA_OPTIONS) VE kullanicinin gercek
# okuma/yazma fonksiyonlari (get_paper_criteria()/set_paper_*_criteria())
# backend/settings.py'ye TASINDI (2026-11-06'da "Ayarlar" diye ayri bir
# ekrana/dosyaya ayrildi, bkz. CLAUDE.md). bist_scanner.py bu dosyayi
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

# BENCHMARK_TICKER/ATR_DAY_MULT/ATR_SWING_MULT/ATR_TARGET_R/WEEKLY_LOOKBACK_DAYS/
# MONTHLY_LOOKBACK_DAYS/YEARLY_LOOKBACK_DAYS/SECTOR_TRANSLATE/BIST30_TICKERS/
# BIST100_TICKERS/BIST_INDEX_LISTS_VERIFIED_ON/MAX_RETRIES/TOP300_SIZE/
# SCREEN_PAGE_SIZE/RETRY_BACKOFF_SECONDS/GRAMS_PER_TROY_OUNCE VE fetch_row/
# fetch_all/fetch_with_retry/fetch_top_gainers/fetch_all_bist_tickers/
# fetch_all_bist_quotes/fetch_market_indicators/get_benchmark_daily_pct/
# _quote_price_and_pct/build_bist_query artik BURADA DEGIL, bist_market_data.py'ye
# tasindi (2026-11-14, bkz. CLAUDE.md). _paper_last_score/_paper_price_issue_
# alert_sent VE PAPER_MAX_HOLD_DAYS/PAPER_MIN_VOL20/run_paper_trading_cycle/
# get_paper_*/manual_buy/manual_sell/reset_paper_trading/build_paper_*_report
# gibi TUM Sanal Al-Sat is mantigi da bist_paper_trading.py'ye tasindi - bist_scanner.py
# bu iki dosyayi HIC import ETMEZ (tersi DAIRESEL IMPORT olurdu), onlar bu
# dosyayi (DB/ortak sabitler icin) TEK YONLU import eder.



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
    """bist_web.py: _run_scan()'in HER satira isaret/not eklemek icin
    kullandigi toplu okuma - DB'ye GITMEZ, bellek-ici cache'in bir KOPYASINI
    doner (cagiran taraf yanlislikla mutasyona ugratmasin diye)."""
    return dict(_ticker_flags_cache)


def set_ticker_flag(ticker: str, flag, note) -> None:
    """Bir ticker'in isaretini/notunu DB'ye yazar VE ayni anda bellek-ici
    cache'i gunceller - bist_web.py'nin API route'u bu cagridan SONRA
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


# _seed_paper_last_score/build_paper_hourly_report/send_paper_hourly_report/
# build_paper_weekly_report/send_paper_weekly_report artik BURADA DEGIL,
# bist_paper_trading.py'ye tasindi (2026-11-14, bkz. CLAUDE.md).



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
    # bir "🛒 SANAL ALIM" bildirimi gonderiliyor (bkz. bist_paper_trading.py:
    # _send_paper_trade_notification). Bu kontrol log_signal_changes()'in
    # signal_log'a YAZMASINI etkilemez - sadece push gonderimi atlanir,
    # tarihsel kayit kesintisiz devam eder.
    # Not: run_paper_trading_cycle() bu fonksiyondan SONRA cagrildigi icin
    # (bkz. bist_web.py: _scan_once()) burada okunan pozisyon listesi
    # bu dongude YENI acilan bir alimi henuz icermez - yani bir hisse GUCLU_AL/
    # MUTLAK_AL'a ILK girdigi dongude (tam alim tetiklendigi an) bildirim HALA
    # gonderilir, SADECE zaten ONCEKI bir dongude acilmis pozisyonlar icin
    # bastirilir.
    #
    # held_tickers/notify_zone ikisi de DOGRUDAN SQL ile okunuyor -
    # get_paper_positions()/get_paper_criteria() bist_paper_trading.py/settings.py'ye
    # TASINDI (bkz. CLAUDE.md), bist_scanner.py bu iki dosyayi DAIRESEL IMPORT
    # olusturmamak icin HIC import etmiyor (onlar bu dosyadaki
    # get_db_connection()/_DB_LOCK'u kullaniyor, yani bagimlilik TEK yonlu).
    conn = get_db_connection()
    try:
        held_tickers = {
            r["ticker"] for r in conn.execute("SELECT DISTINCT ticker FROM paper_trades WHERE status = 'open'")
        }
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
        label = notification._SIGNAL_NOTIFY_LABELS.get(entry["score"], entry["score"])
        text = (
            f"{symbol} {entry['ticker']} -> {label}\n"
            f"{entry['price']:.2f} TL ({entry['daily_pct']:+.2f}%)\n"
            f"{entry['timestamp']}"
        )
        notification.send_telegram_message(text)


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
    yansir, bist_web.py'de EK bir degisiklik gerekmez. Skor
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
    ayri bellek-ici index'ten (bkz. gecmis bist_web.py) okunurdu; artik
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


# run_paper_trading_cycle/get_paper_max_positions/set_paper_max_positions/
# set_paper_cash/reset_paper_trading/get_paper_watchlist/add_to_paper_watchlist/
# remove_from_paper_watchlist/manual_buy/manual_sell/get_paper_positions/
# get_paper_trades/set_paper_trade_note/get_paper_equity_history/
# _compute_paper_performance_metrics/get_paper_benchmark_return/
# get_paper_summary ve PAPER_MAX_POSITIONS_MIN/MAX/PAPER_TRADE_NOTE_MAX_LEN
# artik BURADA DEGIL, bist_paper_trading.py'ye tasindi (2026-11-14, bkz. CLAUDE.md
# "Indicators.py ayristirildi" ve devami).
