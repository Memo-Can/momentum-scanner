"""Yonetici Paneli (bkz. CLAUDE.md "Yonetici Paneli", 2026-11-09'da eklendi)
icin iş katmani - servis durumu (son tarama suresi/hisse sayisi, ardisik
tarama hatasi, piyasa durumu, Telegram kurulumu vb.) bilgisini SSH+journalctl'e
girmeden gorunur kilan popup'in backend tarafi. Dogrudan calistirilmaz.

Bu dosya BILINCLI olarak web_bist.py'den AYRI - settings.py ("Ayarlar
ekrani ayrildi" bolumunde aciklanan gerekceyle) ile AYNI dosya-organizasyonu
ilkesi, SADECE Yonetici Paneli'ne ozel/daha kucuk olceginde: kullanici
"adminle ilgili backend kodlarini ayri bir .py dosyasinda yapmani istiyorum,
projenin okunurlugu acisindan onemli" dedi.

settings.py'den (DB'den okur/yazar) FARKLI bir veri kaynagi durumu var:
servis durumu DB'de DEGIL, web_bist.py'nin kendi bellek-ici tarama
state'inde (_state/_scan_failure_count/_scan_health_alert_sent/
_PROCESS_START_TIME) yasiyor - bu yuzden buradaki fonksiyonlar bu veriyi
PARAMETRE olarak alir, scanner_bist.py'yi (is_market_open()/
TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID icin) import eder ama web_bist.py'yi
HIC import ETMEZ - tersi (bu dosyanin web_bist.py'nin _state'ine
DOGRUDAN erismesi) DAIRESEL IMPORT olurdu. web_bist.py bu modulu
import edip GET /api/admin/status route'unu build_admin_status()'a delege
eder (kendi state'ini toplayip parametre olarak gecirir)."""

import subprocess
import time

import scanner_bist as scanner

# web-bist.service - SABIT, kullanici girdisinden BAGIMSIZ bir deger. Asla
# string birlestirme/f-string ile OLUSTURULMAZ, subprocess'e HER ZAMAN bir
# LISTE olarak (shell=True KULLANILMADAN) geciliyor - komut enjeksiyonu
# riski YOK (bkz. get_recent_error_logs()).
_SYSTEMD_UNIT = "web-bist.service"
# Proje logging modulu/seviye ayrimi KULLANMIYOR - TUM print() cagrilari
# journal'a AYNI ("info") seviyede dusuyor, bu yuzden journalctl'in kendi
# -p (priority) filtresi burada ISE YARAMAZ. Bunun yerine metin bazli bir
# filtre kullanilir - kod tabanindaki TUM print() hata/uyari mesajlari
# (grep ile tek tek tarandi: "Tarama hatasi:", "Sanal al-sat (paper trading)
# hatasi:", "Telegram bildirimi gonderilemedi:", "[UYARI] BIST30/100_TICKERS
# listeleri ... dogrulanmadi" vb.) bu anahtar kelimelerden EN AZ birini
# iceriyor - "hata"/"exception"/"traceback" TEK BASINA "gonderilemedi"
# (basarisiz bir gonderim) ve "[UYARI]" (index listesi tazelik hatirlaticisi)
# gibi mesajlari KACIRIRDI, bu yuzden onlar da eklendi.
_ERROR_KEYWORDS = ("hata", "exception", "traceback", "error", "uyari", "warning", "gonderilemedi", "basarisiz")


def get_recent_error_logs(max_lines: int = 200) -> dict:
    """web-bist.service'in (SADECE bu servis, sistem geneli DEGIL) son
    journalctl kayitlarindan "hata" iceren satirlari filtreler - Yonetici
    Paneli popup'inda SSH+journalctl'e girmeden gosterilir (2026-11-09'da
    eklendi, kullanici istegi: "journalctl'de olusan hata loglarini
    yonetim panelinde gostermek mumkun mu").

    Lokal gelistirme ortaminda (systemd servisi DEGIL, dogrudan
    "python3 web_bist.py" ile calistiriliyor) journalctl/systemctl
    YOK - bu durumda subprocess FileNotFoundError/CalledProcessError
    firlatir, GRACEFUL bir sekilde yakalanip {"available": False, ...}
    doner, panel COKMEZ."""
    try:
        result = subprocess.run(
            ["journalctl", "-u", _SYSTEMD_UNIT, "-n", "1000", "-r", "--no-pager", "-o", "short-iso"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as e:
        return {"available": False, "lines": [], "error": str(e)}
    if result.returncode != 0:
        return {"available": False, "lines": [], "error": (result.stderr or "journalctl başarısız oldu").strip()}
    error_lines = [
        line for line in result.stdout.splitlines()
        if any(k in line.lower() for k in _ERROR_KEYWORDS)
    ]
    return {"available": True, "lines": error_lines[:max_lines]}


def format_uptime_tr(seconds: float) -> str:
    """saniyeyi "3 gün 2 saat" / "5 saat 12 dakika" / "8 dakika" gibi
    okunabilir bir servis-calisma-suresi metnine cevirir - index.html:
    fmtRelativeTime()'in (GECMISE donuk "X once") AKSINE, bu GELECEGE/
    suregelen bir sureyi anlatiyor, o yuzden ayri/basit bir formatlayici
    (saniye hassasiyeti gerekmiyor)."""
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days > 0:
        return f"{days} gün {hours} saat"
    if hours > 0:
        return f"{hours} saat {minutes} dakika"
    if minutes > 0:
        return f"{minutes} dakika"
    return "1 dakikadan az"


def build_admin_status(*, process_start_time, scan_state, scan_failure_count, scan_health_alert_active) -> dict:
    """Yonetici Paneli popup'inin (bkz. _view_admin.html) tek veri kaynagi -
    web_bist.py'nin kendi topladigi anlik state'i (parametreler)
    paketleyip doner. HICBIR yeni Yahoo istegi/DB sorgusu YAPMAZ, sadece
    zaten var olan veriyi yeniden bicimlendirir.

    scan_state: web_bist.py: _state'in (ya da onun bir KOPYASININ -
    _state_lock altinda alinmis) "updated_at"/"last_scan_duration_seconds"/
    "last_scan_ticker_count"/"missing_count" alanlarini iceren sozluk."""
    return {
        "market_open": scanner.is_market_open(),
        "uptime_text": format_uptime_tr(time.time() - process_start_time),
        "last_scan_at": scan_state["updated_at"],
        "last_scan_duration_seconds": scan_state["last_scan_duration_seconds"],
        "last_scan_ticker_count": scan_state["last_scan_ticker_count"],
        "last_scan_missing_count": scan_state["missing_count"],
        "consecutive_scan_failures": scan_failure_count,
        "scan_health_alert_active": scan_health_alert_active,
        "telegram_configured": bool(scanner.TELEGRAM_BOT_TOKEN and scanner.TELEGRAM_CHAT_ID),
    }
