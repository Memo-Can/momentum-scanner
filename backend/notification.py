"""Telegram bildirim KANALININ kendisi - "nasil gonderiyoruz" (bkz. CLAUDE.md
"Telegrama mesaj gonderen kod bloklari ayri dosyaya tasindi") icin iş katmani.
Dogrudan calistirilmaz.

Bu dosya settings.py/admin.py'den FARKLI bir bagimlilik yonunde: onlar
bist_scanner.py'yi import EDER (DB'ye erismek icin), bu dosya ise
bist_scanner.py'yi (ya da projedeki baska hicbir modulu) HIC import ETMEZ -
TAMAMEN bagimsiz bir "yaprak" (leaf) modul: sadece stdlib (os/json/urllib/
datetime) + ortam degiskenleri kullanir. Boylece bist_scanner.py bu dosyayi
GUVENLE import edebilir (DAIRESEL IMPORT riski YOK, cunku bagimlilik SADECE
TEK yonde: bist_scanner.py -> notification.py).

NEDEN raporların (saatlik/haftalik K/Z, sinyal push, alim/satim bildirimi)
METNINI OLUSTURAN fonksiyonlar (build_paper_hourly_report/build_paper_weekly_
report/_send_push_notifications/_send_paper_trade_notification) BURAYA
TASINMADI, hala bist_scanner.py'de: o fonksiyonlar get_paper_positions()/
get_paper_summary()/paper_account tablosunu (DB) OKUMAK zorunda - yani
bist_scanner.py'nin DB katmanina bagimli. Eger onlar da buraya tasinsaydi,
bu dosya bist_scanner.py'yi import ETMEK ZORUNDA kalirdi (DB fonksiyonlari
icin) VE bist_scanner.py de bu dosyayi import ediyor olacagi icin (mesaj
gondermek icin) DAIRESEL IMPORT olusurdu. Bu yuzden ayrim "DB'ye ihtiyaci
olan/olmayan" sinirina gore cizildi: BURADA sadece "bir metni Telegram'a
GONDERME" + metni OLUSTURAN PUR (DB'siz) bicimleme yardimcilari/etiket
sozlukleri var; "hangi veriden hangi metnin uretilecegi" mantigi
bist_scanner.py'de kalip bu modulu CAGIRIR (notification.send_telegram_message(text)).

bist_scanner.py'nin kendi _DB_LOCK'u gibi bir kilit BURADA yok - cunku bu
dosyada hic DB erisimi yok, sadece agag/HTTP istegi var."""

import json
import os
import urllib.request
from datetime import datetime

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def send_telegram_message(text: str) -> None:
    """Telegram bot API'sine HAM bir metin mesaji gonderir - sinyal
    bildirimleri, sanal al-sat alim/satim bildirimleri VE saatlik/haftalik
    rapor gonderimlerinin ORTAK gonderim kodu (eskiden bist_scanner.py:
    _telegram_send() idi, bu dosyaya tasinirken cagiran taraftan GORULEBILIR
    olmasi gerektigi icin public isme (send_telegram_message) cevrildi).
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


def send_health_alert(text: str) -> None:
    """Tarama dongusunun kendi kendini izleyen basit saglik uyarisi -
    bist_web.py: _scan_loop() ardisik birkac basarisiz _scan_once()
    denemesinden sonra, VE bist_scanner.py: run_paper_trading_cycle()
    acik pozisyonlarin fiyati alinamadiginda bunu cagirir. send_telegram_
    message ile AYNI alt yapiyi kullanir (token/chat_id tanimli degilse
    sessizce atlar) - amac sadece "servis sessizce veri cekmeyi durdurdu,
    kimse haberdar degil" durumunu yakalamak, 5 dakikalik Yahoo rate-limit
    dalgalanmalarini degil (tek seferlik hatalar bunu TETIKLEMEZ, sadece
    UST USTE ayni dongude basarisizlik tetikler)."""
    send_telegram_message(f"⚠️ {text}")


# Saatlik K/Z raporundaki her pozisyonun YANINA "GÜÇLÜ AL (3 gün önce)" gibi
# bir baglam eklemek icin (2026-11-06'da eklendi) - 7 skorun TAMAMI icin
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

_SIGNAL_NOTIFY_LABELS = {
    "GUCLU_AL": "GÜÇLÜ AL",
    "MUTLAK_AL": "MUTLAK AL",
    "GUCLU_SAT": "GÜÇLÜ SAT",
    "MUTLAK_SAT": "MUTLAK SAT",
}

_PAPER_EXIT_REASON_LABELS = {
    "stop": "Stop",
    "target": "Hedef",
    "signal_reversal": "Sinyal Tersi",
    "time_stop": "Süre Aşımı",
    "manual_sat_signal": "Sinyal (SAT)",
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


def _format_pct_tr(v) -> str:
    """fmtPct()'in (frontend) DUZ METIN karsiligi - Telegram raporu icin.
    None/NaN icin '-' doner."""
    if v is None or v != v:
        return "-"
    return f"{v:+.2f}%"
