"""Ayarlar ekrani (2026-11-06'da eklendi, bkz. CLAUDE.md) icin iş katmani -
kullanicinin elle sectigi otomatik al/sat kriterleri VE Telegram SKOR
sinyali bildirim kriterini okur/yazar. Dogrudan calistirilmaz.

Bu dosya BILINCLI olarak bist_scanner.py'den AYRI - kullanici "otomatik
alim satim kriterleriyle telegram mesaj kriterlerini birbirinden ayiralim,
bu akisi isleyen backend dosyasi settings.py olmali" dedi. bist_scanner.py'nin
get_db_connection()/_DB_LOCK'unu kullanmak icin BU dosyayi import eder - ters
yonde bir import (bist_scanner.py'nin bunu import etmesi) DAIRESEL
olurdu, bu yuzden bist_scanner.py bu dosyayi HIC import ETMEZ (kriter
okuyan 2 yer - bist_paper_trading.py: run_paper_trading_cycle()/bist_scanner.py:
_send_push_notifications() - paper_account sutunlarini DOGRUDAN SQL ile
okur, buradaki fonksiyonlari CAGIRMAZ).

bist_web.py bu modulu import edip /api/settings/criteria route'unu
buradaki fonksiyonlara delege eder (bist_web.py "WEB sunum katmani",
bu dosya "is katmani" - bist_scanner.py/bist_web.py ayrimiyla
AYNI mimari ilke, SADECE Ayarlar ekranina ozel/daha kucuk olceginde)."""

import os
import sys

# bist_scanner.py artik backend/bist/ icinde (2026-11-14'te tasindi, bkz.
# CLAUDE.md "Dosyalar bist/ klasorune tasindi") - bu dosya backend/'de
# KALDIGI icin onu bulabilmek icin backend/bist/ sys.path'e eklenir.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "bist"))

import bist_scanner as scanner  # noqa: E402

# Kullanicinin secebilecegi TEK gecerli etiket kumeleri - AL tarafinda SAT
# etiketlerinin secilmesi anlamsiz oldugu icin (otomatik alim SADECE bu yone
# ozgu bir strateji) kasitli olarak sinirlandirildi. "STOP"/"TARGET" GERCEK
# skor etiketleri DEGIL - fiyat-bazli stop-loss/hedef kontrolunu
# (paper_trading.run_paper_trading_cycle()) ac/kapa yapan birer SAHTE ("pseudo")
# etiket. "NOTR" SAT tarafina 2026-11-10'da eklendi (kullanici istegi:
# "ayarlarda otomatik sat kriterine notr kriterini de ekleyelim") - secilirse
# skoru NOTR'e DONEN (GUCLU_AL/AL'dan dusen) bir pozisyon da otomatik
# satilir. VARSAYILAN olarak (_DEFAULT_SELL_ZONE, bist_scanner.py)
# SECILI DEGIL - bu DAVRANIS DEGISIKLIGI mevcut hesaplari ETKILEMEZ, kullanici
# Ayarlar'dan ELLE isaretlerse devreye girer.
PAPER_BUY_CRITERIA_OPTIONS = ("AL", "GUCLU_AL", "MUTLAK_AL")
PAPER_SELL_CRITERIA_OPTIONS = ("SAT", "GUCLU_SAT", "MUTLAK_SAT", "NOTR", "STOP", "TARGET")
# Genel SKOR sinyali Telegram bildirimini (scanner._send_push_notifications,
# 🟢/🔴 ile gelen "X hissesi GUCLU AL'a gecti" mesajlari) tetikleyen etiketler -
# otomatik alim/satim kriterinden (PAPER_BUY_CRITERIA_OPTIONS/PAPER_SELL_CRITERIA_OPTIONS)
# TAMAMEN BAGIMSIZ, kendi DB sutunu (notify_criteria) var.
PAPER_NOTIFY_CRITERIA_OPTIONS = ("GUCLU_AL", "MUTLAK_AL", "GUCLU_SAT", "MUTLAK_SAT")


def get_paper_criteria() -> dict:
    """Kullanicinin elle sectigi UC AYRI kriter kumesini DB'den okur:
    "buy"/"sell" (otomatik alim/satimi tetikler, paper_trading.run_paper_trading_cycle()
    kullanir) ve "notify" (SADECE Telegram SKOR sinyali bildirimini
    tetikler, scanner._send_push_notifications() kullanir - buy/sell'den
    BILINCLI olarak AYRI). Bos liste (kullanici bir grubun TUMUNU bilerek
    secmemis) o yonde (alim/satim/bildirim) hic islem yapilmadigi anlamina
    gelir, bu KASITLI (kullanicinin kendi secimi)."""
    conn = scanner.get_db_connection()
    try:
        row = conn.execute(
            "SELECT buy_criteria, sell_criteria, notify_criteria FROM paper_account WHERE id = 1"
        ).fetchone()
        if row is None:
            return {
                "buy": list(scanner._STRONG_BUY_ZONE),
                "sell": list(scanner._DEFAULT_SELL_ZONE),
                "notify": list(scanner._DEFAULT_NOTIFY_ZONE),
            }
        return {
            "buy": [s for s in row["buy_criteria"].split(",") if s],
            "sell": [s for s in row["sell_criteria"].split(",") if s],
            "notify": [s for s in row["notify_criteria"].split(",") if s],
        }
    finally:
        conn.close()


def _clean_criteria_labels(labels, allowed) -> str:
    """Verilen etiket listesini `allowed` kumesine gore filtreler (gecersiz
    bir etiket sessizce ATILIR, hata FIRLATILMAZ - cagiran taraf/API zaten
    kendi dogrulamasini yapmali), tekrarlari sirayi koruyarak eler ve
    DB'ye yazilacak virgullu string'e cevirir."""
    seen = set()
    result = []
    for label in labels:
        if label in allowed and label not in seen:
            seen.add(label)
            result.append(label)
    return ",".join(result)


def set_paper_buy_criteria(labels) -> None:
    """Otomatik ALIM'i tetikleyen skor etiket kumesini degistirir -
    PAPER_BUY_CRITERIA_OPTIONS disindaki degerler sessizce elenir. Mevcut
    ACIK pozisyonlara dokunmaz, sadece paper_trading.run_paper_trading_cycle()'in
    bundan sonraki dongulerde hangi YENI gecisleri alim adayi sayacagini
    degistirir."""
    value = _clean_criteria_labels(labels, PAPER_BUY_CRITERIA_OPTIONS)
    with scanner._DB_LOCK:
        conn = scanner.get_db_connection()
        try:
            conn.execute("UPDATE paper_account SET buy_criteria = ? WHERE id = 1", (value,))
            conn.commit()
        finally:
            conn.close()


def set_paper_sell_criteria(labels) -> None:
    """Otomatik SATIM'i tetikleyen kriter kumesini degistirir -
    PAPER_SELL_CRITERIA_OPTIONS disindaki degerler sessizce elenir. Skor
    etiketleri (SAT/GUCLU_SAT/MUTLAK_SAT) hem auto HEM manual acik
    pozisyonlari ETKILER. "STOP"/"TARGET" (GERCEK skor DEGIL) ise SADECE
    auto pozisyonlarin fiyat-bazli stop/hedef kontrolunu ac/kapa yapar
    (manual'de zaten hep NULL). Sure asimi kurali bundan BAGIMSIZ, hep
    aktif (SADECE auto)."""
    value = _clean_criteria_labels(labels, PAPER_SELL_CRITERIA_OPTIONS)
    with scanner._DB_LOCK:
        conn = scanner.get_db_connection()
        try:
            conn.execute("UPDATE paper_account SET sell_criteria = ? WHERE id = 1", (value,))
            conn.commit()
        finally:
            conn.close()


def set_paper_notify_criteria(labels) -> None:
    """Telegram SKOR sinyali bildirimini (scanner._send_push_notifications)
    tetikleyen etiket kumesini degistirir - PAPER_NOTIFY_CRITERIA_OPTIONS
    disindaki degerler sessizce elenir. buy_criteria/sell_criteria'dan
    TAMAMEN BAGIMSIZ - bu ayari degistirmek otomatik alim/satim davranisini
    HIC ETKILEMEZ, SADECE Telegram'a gidip gitmeyecegini belirler."""
    value = _clean_criteria_labels(labels, PAPER_NOTIFY_CRITERIA_OPTIONS)
    with scanner._DB_LOCK:
        conn = scanner.get_db_connection()
        try:
            conn.execute("UPDATE paper_account SET notify_criteria = ? WHERE id = 1", (value,))
            conn.commit()
        finally:
            conn.close()
