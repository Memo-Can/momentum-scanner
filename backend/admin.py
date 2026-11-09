"""Yonetici Paneli (bkz. CLAUDE.md "Yonetici Paneli", 2026-11-09'da eklendi)
icin iş katmani - servis durumu (son tarama suresi/hisse sayisi, ardisik
tarama hatasi, piyasa durumu, Telegram kurulumu vb.) bilgisini SSH+journalctl'e
girmeden gorunur kilan popup'in backend tarafi. Dogrudan calistirilmaz.

Bu dosya BILINCLI olarak web_bistTop100.py'den AYRI - settings.py ("Ayarlar
ekrani ayrildi" bolumunde aciklanan gerekceyle) ile AYNI dosya-organizasyonu
ilkesi, SADECE Yonetici Paneli'ne ozel/daha kucuk olceginde: kullanici
"adminle ilgili backend kodlarini ayri bir .py dosyasinda yapmani istiyorum,
projenin okunurlugu acisindan onemli" dedi.

settings.py'den (DB'den okur/yazar) FARKLI bir veri kaynagi durumu var:
servis durumu DB'de DEGIL, web_bistTop100.py'nin kendi bellek-ici tarama
state'inde (_state/_scan_failure_count/_scan_health_alert_sent/
_PROCESS_START_TIME) yasiyor - bu yuzden buradaki fonksiyonlar bu veriyi
PARAMETRE olarak alir, scanner_bistTop100.py'yi (is_market_open()/
TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID icin) import eder ama web_bistTop100.py'yi
HIC import ETMEZ - tersi (bu dosyanin web_bistTop100.py'nin _state'ine
DOGRUDAN erismesi) DAIRESEL IMPORT olurdu. web_bistTop100.py bu modulu
import edip GET /api/admin/status route'unu build_admin_status()'a delege
eder (kendi state'ini toplayip parametre olarak gecirir)."""

import time

import scanner_bistTop100 as scanner


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
    web_bistTop100.py'nin kendi topladigi anlik state'i (parametreler)
    paketleyip doner. HICBIR yeni Yahoo istegi/DB sorgusu YAPMAZ, sadece
    zaten var olan veriyi yeniden bicimlendirir.

    scan_state: web_bistTop100.py: _state'in (ya da onun bir KOPYASININ -
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
