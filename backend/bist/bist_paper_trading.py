"""Sanal para (paper trading / Sanal Al-Sat) simulasyonu - Konfluens Skor'un
gercekte bir edge saglayip saglamadigini kendi canli verimizle ampirik
olcmek icin (bkz. CLAUDE.md "Sanal para (paper trading) simulasyonu"). Bu,
"Basta hangi dosyalar olusturulabilir" analizinin (bist_indicators.py/
notification.py/bist_market_data.py'nin ardindan) en buyuk parcasi.
Dogrudan calistirilmaz.

`settings.py`/`admin.py`/`bist_market_data.py` ile AYNI mimari ilke: bu dosya
`bist_scanner.py`'yi TEK YONLU import eder (get_db_connection()/_DB_LOCK
icin - DB SEMASI/tablo olusturma HALA bist_scanner.py'de, cunku o TUM
projenin DB kurulumunu yapiyor, sadece paper-trading'in degil).
`bist_scanner.py` bu dosyayi HIC import ETMEZ - tersi DAIRESEL IMPORT
olurdu. `notification.py`'yi (Telegram gonderimi) ve `bist_market_data.py`'yi
(BENCHMARK_TICKER - BIST100 karsilastirmasi icin) de import eder, ikisi de
TEK yonlu, sorunsuz.

⚠️ bist_scanner.py'de KALAN `_send_push_notifications()` (genel SKOR sinyali
Telegram bildirimi, log_signal_changes()'ten cagrilir) burasi TASINMADAN
ONCE `get_paper_positions()`'i CAGIRIYORDU - bu fonksiyon buraya tasininca
bist_scanner.py'nin bu dosyayi import etmesi gerekirdi ki bu DAIRESEL olurdu.
Cozum: _send_push_notifications() artik "acik pozisyon var mi" sorusunu
KENDI DOGRUDAN SQL sorgusuyla (`SELECT DISTINCT ticker FROM paper_trades
WHERE status='open'`) cevapliyor - settings.py'nin kriter sutunlarini
DOGRUDAN SQL ile okuyup bu modulu ÇAGIRMAMASIYLA AYNI desen."""

import os
import sys
import time
from datetime import datetime, timedelta

import yfinance as yf

import bist_indicators as indicators
import bist_market_data as market_data

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import notification  # noqa: E402
import bist_scanner as scanner  # noqa: E402

PAPER_MAX_HOLD_DAYS = 21  # ~15 islem gunune denk takvim gunu yaklasimi
# Otomatik alim icin minimum volatilite filtresi (2026-10-30, backtest sonucu
# eklendi): "row['vol20']" (gunluk kapanis getirilerinin son 20 gunluk standart
# sapmasi) bu esigin ALTINDAYSA GUCLU_AL gecisi otomatik alinmaz - 1 yillik
# backfill verisiyle yapilan test, en sakin %25'lik dilimdeki hisselerde
# GUCLU_AL'in pratikte edge tasimadigini (ortalama getiri ~%0.16, diger
# ceyreklerde %1.3-2.2) gosterdi. Esik, o testteki Q1 ustu (25. persentil)
# degeri (%2.13) - bkz. CLAUDE.md "GUCLU_AL volatilite filtresi - backtest".
# SADECE otomatik alim kararini etkiler - GUCLU_AL etiketi/gorunumu (sitede
# herkese gosterilen) DEGISMEZ, indicators.confluence_label() bundan habersiz.
PAPER_MIN_VOL20 = 0.0213
PAPER_MAX_POSITIONS_MIN = 1
PAPER_MAX_POSITIONS_MAX = 50
PAPER_TRADE_NOTE_MAX_LEN = 250

_paper_last_score = {}
# Rate-limit/veri-eksikligi (Yahoo'dan bir veya daha fazla acik pozisyonun
# fiyatinin bu dongude gelmemesi) durumunda TEK bir Telegram uyarisi
# gonderilsin - bist_web.py: _scan_health_alert_sent ile AYNI desen,
# sorun surerken her dongude (5dk'da bir) tekrar tekrar SPAM edilmesin, sorun
# duzelince (bir sonraki temiz dongude) tekrar False'a donup gelecekte
# yeniden tetiklenebilsin diye. bkz. run_paper_trading_cycle().
_paper_price_issue_alert_sent = False
_paper_benchmark_cache = {}  # "data" -> (fetched_at, since_date, pct)


def _seed_paper_last_score() -> None:
    """Process (re)basladiginda _paper_last_score'u last_known_score
    tablosundan doldurur - bist_scanner.py: _seed_last_logged_scores() ile
    AYNI amac/desen (restart'ta zaten GUCLU_AL/MUTLAK_AL durumunda olan
    hisseler YANLISLIKLA 'yeni gecis' sayilip otomatik satin ALINMASIN).
    Eskiden _paper_last_score her restart'ta BOMBOS basliyordu, sadece
    "ilk dongude prev=None, hicbir sey tetiklenmez" korumasina
    guveniliyordu - ama bu korumanin YAN ETKISI, dinamik BIST 300 havuzuna
    HER GUN yeni giren bir hissenin (ilk kez gorulduyor, prev=None) ZATEN
    GUCLU_AL/MUTLAK_AL olarak havuza girmesi durumunda SONSUZA KADAR
    atlanmasiydi (sadece restart'ta degil, HER yeni havuz girisinde) -
    2026-11-02'de gercekten yasandi/bulundu (bkz. CLAUDE.md).
    last_known_score'dan seed etmek restart korumasini ayni sekilde
    saglarken, gercekten YENI (hic gorulmemis) bir hisseyi artik
    kisitlamiyor - bkz. asagidaki run_paper_trading_cycle'daki degisiklik."""
    conn = scanner.get_db_connection()
    try:
        rows = conn.execute("SELECT ticker, score FROM last_known_score").fetchall()
        for row in rows:
            _paper_last_score[row["ticker"]] = row["score"]
    finally:
        conn.close()


_seed_paper_last_score()


def run_paper_trading_cycle(rows) -> None:
    """Sanal para (paper trading) simulasyonunun ana dongusu -
    bist_web.py: _scan_once() tarafindan HER tarama dongusunde
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

    with scanner._DB_LOCK:
        conn = scanner.get_db_connection()
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
            # bist_web.py: _scan_once()'taki _sort_key), bu yuzden
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
            notification.send_health_alert(
                "Sanal Al-Sat: bir veya daha fazla acik pozisyonun fiyati bu "
                "dongude alinamadi (rate limit veya baska bir veri sorunu "
                "olabilir). Etkilenen pozisyonlarin K/Z'si bu dongude NOTR "
                "(degisim yok) kabul edildi, yanlis bir kayip raporlanmadi."
            )
            _paper_price_issue_alert_sent = True
    else:
        _paper_price_issue_alert_sent = False


def get_paper_max_positions() -> int:
    """Kullanicinin elle ayarladigi maksimum acik pozisyon slotunu DB'den
    okur (2026-11-06'da eklendi, bkz. CLAUDE.md) - eskiden sabit
    PAPER_MAX_POSITIONS=5 idi, artik Sanal Al-Sat panelinden degistirilebilir.
    run_paper_trading_cycle()/manual_buy() her cagrida bunu tazeler (tek
    satirlik sorgu, pahali degil) - deger degisse de ayri bir restart/cache
    temizligi GEREKMEZ."""
    conn = scanner.get_db_connection()
    try:
        row = conn.execute("SELECT max_positions FROM paper_account WHERE id = 1").fetchone()
        return row["max_positions"] if row else scanner.PAPER_MAX_POSITIONS
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
    with scanner._DB_LOCK:
        conn = scanner.get_db_connection()
        try:
            conn.execute("UPDATE paper_account SET max_positions = ? WHERE id = 1", (n,))
            conn.commit()
        finally:
            conn.close()


def set_paper_cash(amount: float) -> None:
    """Sanal nakti elle istenen tutara ayarlar - kullanici Sanal Al-Sat
    panelinden 'Nakit' degerini duzenleyebiliyor. Negatif deger cagiran
    tarafta (API) reddedilmeli, burada sadece yazilir."""
    with scanner._DB_LOCK:
        conn = scanner.get_db_connection()
        try:
            conn.execute("UPDATE paper_account SET cash = ? WHERE id = 1", (amount,))
            conn.commit()
        finally:
            conn.close()


def reset_paper_trading(starting_balance: float | None = None) -> None:
    """Sanal al-sat'i sifirlar: ACIK islemler, equity gecmisi ve manuel
    takip listesi SILINIR. starting_balance VERILIRSE nakit VE
    starting_balance (toplam K/Z'nin olculdugu baz) o degere esitlenir -
    sabit PAPER_STARTING_BALANCE kullanilmaz, kullanici kendi baslangic
    tutarini secebilir. starting_balance VERILMEZSE (None, varsayilan,
    2026-11-12'de eklendi - kullanici istegi: "herseyi sifirladigimizda
    otomatik olarak nakit girmesine gerek yok") mevcut cash/starting_balance'a
    HIC DOKUNULMAZ - kullanici artik her sifirlamada yeni bir tutar girmek
    ZORUNDA degil, sadece ACIK pozisyonlar/gecmis/takip listesi temizlenir.
    Frontend'de bu, "emin misin" onayi SONRASINDA cagrilir. _paper_last_score
    (hangi ticker'in GUCLU_AL'a yeni gectigini takip eden hafiza) KASITLI
    olarak DOKUNULMAZ - yoksa sifirlama hemen ardindan, zaten GUCLU_AL'da
    olan tum hisseleri 'yeni gecis' sayip ayni restart-hatasini (bkz.
    CLAUDE.md) tekrar tetikler.

    ⚠️ KAPANAN (status='closed') islemler ARTIK SILINMIYOR (2026-11-05'te
    kullanici istegiyle degistirildi - "nakit sifirlanirken kapanan
    islemler hicbir zaman sifirlanmasin") - gecmis performans/aciklama
    notlari (bkz. note sutunu) kalici bir kayit olarak korunuyor, sadece
    ACIK pozisyonlar (henuz sonuclanmamis, yeni baslangic sermayesiyle
    artik anlamsiz kalan) temizleniyor."""
    with scanner._DB_LOCK:
        conn = scanner.get_db_connection()
        try:
            conn.execute("DELETE FROM paper_trades WHERE status = 'open'")
            conn.execute("DELETE FROM paper_equity_history")
            conn.execute("DELETE FROM paper_watchlist")
            if starting_balance is not None:
                conn.execute(
                    "UPDATE paper_account SET cash = ?, starting_balance = ? WHERE id = 1",
                    (starting_balance, starting_balance),
                )
            conn.commit()
        finally:
            conn.close()


def get_paper_watchlist():
    """Kullanicinin elle ekledigi, otomatik sistemden BAGIMSIZ manuel takip
    listesini dondurur - bist_web.py: _scan_once() bu ticker'lari da
    (acik pozisyonlar gibi) tarama havuzuna ekler, boylece fiyatsiz kalmaz."""
    conn = scanner.get_db_connection()
    try:
        cur = conn.execute("SELECT ticker, added_at FROM paper_watchlist ORDER BY added_at DESC")
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def add_to_paper_watchlist(ticker: str) -> None:
    with scanner._DB_LOCK:
        conn = scanner.get_db_connection()
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
    with scanner._DB_LOCK:
        conn = scanner.get_db_connection()
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
    hedef degerleri, cagiran (bist_web.py) tarafindan _state["rows"]'tan
    okunup buraya gecirilir - bu fonksiyon Yahoo'ya kendisi HIC ISTEK ATMAZ.
    None/NaN gecilirse (ATR verisi yetersiz - yeni listelenen bir hisse ya da
    veri sorunu) stop_price/target_price eskisi gibi NULL yazilir, alim
    REDDEDILMEZ - kullanici BILEREK elle aliyor, auto'nun aksine (orada
    swing_stop/target yoksa aday TAMAMEN atlanir) burada bu EKSIKLIK alimi
    bloke etmemeli."""
    if allocation <= 0 or price != price or price <= 0:
        return None
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with scanner._DB_LOCK:
        conn = scanner.get_db_connection()
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
    with scanner._DB_LOCK:
        conn = scanner.get_db_connection()
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
    guncel fiyat/K-Z hesaplamasi bist_web.py'de canli rows ile
    birlestirilerek yapilir)."""
    conn = scanner.get_db_connection()
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
    conn = scanner.get_db_connection()
    try:
        cur = conn.execute(
            "SELECT * FROM paper_trades WHERE status = 'closed' ORDER BY exit_time DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


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
    with scanner._DB_LOCK:
        conn = scanner.get_db_connection()
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
    conn = scanner.get_db_connection()
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


def get_paper_benchmark_return(since_timestamp):
    """paper_equity_history'nin EN ESKI (ilk) satirinin tarihinden (since_timestamp,
    "YYYY-MM-DD HH:MM:SS") BUGUNE kadar market_data.BENCHMARK_TICKER'in
    (XU100.IS, RS karsilastirmasinda da kullanilan AYNI BIST100 endeksi)
    toplam yuzde degisimini dondurur - Sanal Al-Sat performansini "sadece
    al-ve-tut yapsaydim ne olurdu" ile kiyaslamak icin (2026-11-07'de
    eklendi, bkz. CLAUDE.md "BIST100 benchmark karsilastirmasi").
    since_timestamp None ise (hic equity kaydi yoksa - orn. taze/sifirlanmis
    hesap) karsilastirilacak bir donem olmadigi icin None doner.
    `market_data._benchmark_cache`/`market_data.get_benchmark_daily_pct()`
    SADECE bugunku gunluk % degisimi onbelleklerken, bu fonksiyon BASLANGIC
    gununden BUGUNE kadarki TOPLAM degisimi tutar - ayri bir onbellek
    GEREKTI. `_chart_cache`/`/api/history`'nin lazy-fetch-with-TTL
    deseniyle AYNI felsefe: Flask route'undan (get_paper_summary()
    uzerinden) DOGRUDAN, senkron cagrilir (arka plan tarama donguusune
    TASINMADI - /api/indicators de zaten ayni sekilde route icinde canli
    Yahoo cekiyor) ama TTL = BIST_REFRESH_SECONDS-5 oldugu icin ayni 5
    dakikalik pencerede gelen TUM istekler (kac sekme/kullanici olursa
    olsun) PAYLASILAN tek bir Yahoo cagrisini kullanir."""
    if not since_timestamp:
        return None
    since_date = since_timestamp[:10]
    cached = _paper_benchmark_cache.get("data")
    if cached and cached[1] == since_date and time.time() - cached[0] < scanner.BIST_REFRESH_SECONDS - 5:
        return cached[2]
    pct = None
    try:
        tk = yf.Ticker(market_data.BENCHMARK_TICKER)
        hist = tk.history(start=since_date, interval="1d", auto_adjust=False)
        if len(hist) >= 1:
            start_price = float(hist["Close"].iloc[0])
            current_price = float(hist["Close"].iloc[-1])
            pct = indicators.pct_change(current_price, start_price)
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
    conn = scanner.get_db_connection()
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
        # olamaz (_init_paper_trading() hep pozitif bir deger yazar, reset_paper_trading()
        # starting_balance=None iken DOKUNMAZ - yani hep ONCEKI pozitif deger kalir) ama
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


def build_paper_hourly_report(rows) -> str:
    """Su an acik olan TUM sanal pozisyonlari (kaynak farki YOK, auto+manual)
    rows icindeki (zaten taranmis, EKSTRA Yahoo istegi YOK) canli fiyatla
    birlestirip saat basi bir K/Z rapor metni olusturur. Hem
    bist_web.py: _maybe_send_hourly_paper_report() (gercek saatlik
    tetikleyici, bkz. orada) HEM DE /api/paper/report-test (anlik test
    tetikleyicisi) tarafindan cagirilir - ikisi de AYNI formati gorsun diye
    rapor metni ve gonderimi (send_paper_hourly_report) ayri tutuldu.

    Her pozisyonun yanina GUNCEL skoru ve bu skorun NE ZAMANDAN BERI aktif
    oldugunu (rows zaten scanner.log_score_history()'den GECTIGI icin her
    satirda row["score"]/row["score_since"] hazir - EK bir Yahoo/DB sorgusu
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
            score_label = notification._SCORE_TEXT_LABELS.get(score, score)
            since_text = notification._format_relative_tr(score_since_by_ticker.get(p["ticker"]))
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
    notification.send_telegram_message(text)
    return text


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
    drawdown_pct_text = notification._format_pct_tr(-s["max_drawdown_pct"]) if s["max_drawdown_pct"] else notification._format_pct_tr(0.0)
    sharpe_text = "-" if s["sharpe_ratio"] is None else f"{s['sharpe_ratio']:.2f}"
    profit_factor_text = "-" if s["profit_factor"] is None else f"{s['profit_factor']:.2f}x"
    avg_win_text = "-" if s["avg_win"] is None else f"{s['avg_win']:+,.0f} TL"
    avg_loss_text = "-" if s["avg_loss"] is None else f"{s['avg_loss']:+,.0f} TL"
    lines = [
        f"📈 Haftalık Performans Raporu ({datetime.now().strftime('%Y-%m-%d')})",
        "",
        f"Toplam Değer: {s['equity']:,.0f} TL",
        f"Toplam K/Z: {s['total_pnl']:+,.0f} TL ({notification._format_pct_tr(total_pct)})",
        f"Açık Pozisyon: {s['open_positions']} / {s['max_positions']} | Kapanan İşlem: {s['closed_trades']} | Kazanma Oranı: {win_rate_text}",
        "",
        f"Maks. Drawdown: {s['max_drawdown']:,.0f} TL ({drawdown_pct_text})",
        f"Sharpe Oranı: {sharpe_text}",
        f"Profit Factor: {profit_factor_text}",
        f"Ort. Kazanç/Kayıp: {avg_win_text} / {avg_loss_text}",
        "",
        f"BIST100 (Aynı Dönem): {notification._format_pct_tr(s['benchmark_return_pct'])}",
        f"Alfa (BIST100'e Göre): {notification._format_pct_tr(s['alpha_pct'])}",
    ]
    return "\n".join(lines)


def send_paper_weekly_report() -> str:
    """build_paper_weekly_report()'u olusturup Telegram'a gonderir, metni
    de geri dondurur - send_paper_hourly_report() ile AYNI desen (test
    endpoint'i Telegram kurulu olmasa da metni gorebilsin diye)."""
    text = build_paper_weekly_report()
    notification.send_telegram_message(text)
    return text


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
        reason = notification._PAPER_EXIT_REASON_LABELS.get(event["exit_reason"], event["exit_reason"])
        text = (
            f"{symbol} SANAL SATIŞ ({sonuc}): {event['ticker']}\n"
            f"Giriş {event['entry_price']:.2f} -> Çıkış {event['exit_price']:.2f} TL\n"
            f"K/Z: {pnl:+.0f} TL - Neden: {reason}\n"
            f"{event['timestamp']}"
        )
    notification.send_telegram_message(text)
