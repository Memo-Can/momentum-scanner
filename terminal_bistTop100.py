#!/usr/bin/env python3
"""scanner_bistTop100.py'nin urettigi veriyi terminalde renkli bir tablo
olarak gosterir - is katmani (veri cekme/indikator/skorlama) OLDUGU GIBI
kullanilir, bu dosya sadece terminale ozel sunum (render/renk/ses/dongu)
katmanidir. web_bistTop100.py'nin ayni is katmanini web icin nasil
sundugunun terminal karsiligidir.

Calistirmak icin (proje kok dizininden):
    python3 terminal_bistTop100.py

Her REFRESH_SECONDS (300sn / 5dk) saniyede bir hem tarayici listesi hem de
getiriler Yahoo Finance'ten yeniden cekilip terminal yenilenir.

Sutunlar:
  #        Siralamadaki yeri: once Skor gucu (GUCLU AL en ustte, GUCLU SAT en
           altta), ayni skor grubu icinde gunluk getiriye gore buyukten kucuge
  Skor     1d/5m EMA, MACD, RSI, BB% ve RS'nin tek bir konfluens skoruna
           (-6..+6) birlestirilmis hali: GUCLU AL(▲▲)/AL(▲)/NOTR(–)/SAT(▼)/
           GUCLU SAT(▼▼). 6 ayri sinyale tek tek bakmak yerine tek bakista
           netlik saglar. ADX(14) ayri bir sutun olarak gosterilmez, GUCLU
           AL/SAT etiketini teyit eden bir guven filtresi olarak kullanilir:
           ADX<20 (zayif/yatay piyasa) ise "guclu" etiket otomatik normal
           AL/SAT'a duser.
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
  RS       Goreceli guc: hissenin gunluk % degisimi eksi BIST100 endeksinin
           (XU100.IS) gunluk % degisimi. Endeks +%4 oldugu bir gunde hissenin
           +%5 olmasi, endeksin duz oldugu bir gunde +%5 olmasindan cok daha
           az anlamlidir - RS bu ayrimi sayisallastirir. |RS| < 0.5 NOTR.
  Gunluk   Canli fiyat ile Yahoo'nun canli "onceki kapanis" alani arasindaki
           yuzde fark (satir rengi buna gore yesil/kirmizi olur, liste bu
           sutuna gore buyukten kucuge siralanir)
  GunStop/Hedef  ATR(14)*1.5 mesafeli, sinyal yonune (Skor'un AL/SAT egilimine)
           gore gunluk (day trade) stop-loss/hedef fiyat cifti. Hedef mesafesi
           stop mesafesinin 2 kati (risk:odul ~1:2). Skor NOTR ise "-".
  SwStop/Hedef  Ayni mantik, ATR(14)*3.0 mesafeli - birkac gun/hafta tasinacak
           swing pozisyonlar icin daha genis stop/hedef.

Ses: Her basarili yenilemede (REFRESH_SECONDS'ta bir) tek bir bip sesi
(al_beep.wav) calinir - sinyale bagli degildir.
"""

import os
import subprocess
import sys
import time
from datetime import datetime

import scanner_bistTop100 as scanner

SOUND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sounds")
BEEP_SOUND = os.path.join(SOUND_DIR, "al_beep.wav")

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
BOLD_GREEN = "\033[1;32m"
BOLD_RED = "\033[1;31m"
RESET = "\033[0m"

SIGNAL_SYMBOLS = {"AL": "▲", "SAT": "▼", "NOTR": "–"}
SIGNAL_COLORS = {"AL": GREEN, "SAT": RED, "NOTR": YELLOW}

SCORE_SYMBOLS = {"GUCLU_AL": "▲▲", "AL": "▲", "NOTR": "–", "SAT": "▼", "GUCLU_SAT": "▼▼"}
SCORE_COLORS = {
    "GUCLU_AL": BOLD_GREEN,
    "AL": GREEN,
    "NOTR": YELLOW,
    "SAT": RED,
    "GUCLU_SAT": BOLD_RED,
}


def play_beep(path: str):
    try:
        subprocess.Popen(["aplay", "-q", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def play_refresh_beep():
    play_beep(BEEP_SOUND)


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


def fmt_stop_target(stop, target) -> str:
    """Stop-loss/hedef ciftini 'stop/hedef' seklinde tek bir kompakt metne
    cevirir. Yon belirsizse (NOTR sinyal) ikisi de None olur, '-' gosterilir."""
    if stop is None or target is None:
        return "-"
    return f"{stop:.2f}/{target:.2f}"


def render(rows, missing_count: int = 0):
    use_color = sys.stdout.isatty()
    if use_color:
        os.system("cls" if os.name == "nt" else "clear")

    def _sort_key(row):
        daily = row["daily"]
        daily_sort = -daily if daily == daily else float("inf")  # NaN'lari sona at
        return (scanner.SCORE_ORDER.get(row["score"], 2), daily_sort)

    rows_sorted = sorted(rows, key=_sort_key)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"BIST 100 - Getiri Tablosu ({len(rows)} hisse)   (guncelleme: {now})")
    print(
        f"Kaynak: Yahoo taramasi (region=tr, filtresiz) - gunluk % degisime gore ilk {scanner.TOP_N}   |  "
        f"Siralama: Once Skor gucu (GUCLU AL->AL->NOTR->SAT->GUCLU SAT), ayni grup icinde gunluk % (yuksekten dusuge)"
    )
    print(
        f"Sinyal: EMA{scanner.EMA_FAST}/EMA{scanner.EMA_SLOW} (1d/5dk) + "
        f"MACD({scanner.MACD_FAST},{scanner.MACD_SLOW},{scanner.MACD_SIGNAL}) + RSI({scanner.RSI_PERIOD}) + "
        f"BB({scanner.BB_PERIOD},{scanner.BB_STD}sigma)   |  her {scanner.REFRESH_SECONDS} sn'de bir yenilenir"
    )
    if missing_count:
        print(f"Uyari: Hedeflenen {scanner.TOP_N} hisseden {missing_count} tanesi icin veri alinamadi.")
    print()

    header = (
        f"{'#':>2} {'Skor':^4} {'1d':^2} {'5m':^2} {'MACD':^4} {'Hisse':<5} {'Sektor':<6} {'Fiyat':>6} "
        f"{'Hacim':>5} {'GunPoz':>6} {'RSI':>3} {'BB%':>4} {'RS':>8} {'Gunluk':>8} "
        f"{'GunStop/Hedef':>13} {'SwStop/Hedef':>13}"
    )
    print(header)
    print("-" * len(header))

    for i, row in enumerate(rows_sorted, start=1):
        symbol_score = f"{SCORE_SYMBOLS.get(row['score'], row['score']):^4}"
        symbol_1d = f"{SIGNAL_SYMBOLS.get(row['signal_1d'], row['signal_1d']):^2}"
        symbol_5m = f"{SIGNAL_SYMBOLS.get(row['signal_5m'], row['signal_5m']):^2}"
        symbol_macd = f"{SIGNAL_SYMBOLS.get(row['signal_macd'], row['signal_macd']):^4}"
        idx = f"{i:>2}"
        prefix = (
            f"{row['ticker']:<5} {row['sector']:<6} {row['price']:>6.2f} "
            f"{fmt_ratio(row['volume_ratio']):>5} {fmt_range_pos(row['day_range_pos']):>6} "
            f"{fmt_rsi(row['rsi']):>3} {fmt_range_pos(row['bb_percent']):>4} "
            f"{fmt_pct(row['rel_strength']):>8} {fmt_pct(row['daily']):>8} "
            f"{fmt_stop_target(row['day_stop'], row['day_target']):>13} "
            f"{fmt_stop_target(row['swing_stop'], row['swing_target']):>13}"
        )
        if use_color:
            row_color = GREEN if row["daily"] >= 0 else RED
            idx = f"{row_color}{idx}{RESET}"
            symbol_score = f"{SCORE_COLORS.get(row['score'], YELLOW)}{symbol_score}{RESET}"
            symbol_1d = f"{SIGNAL_COLORS.get(row['signal_1d'], YELLOW)}{symbol_1d}{RESET}"
            symbol_5m = f"{SIGNAL_COLORS.get(row['signal_5m'], YELLOW)}{symbol_5m}{RESET}"
            symbol_macd = f"{SIGNAL_COLORS.get(row['signal_macd'], YELLOW)}{symbol_macd}{RESET}"
            prefix = f"{row_color}{prefix}{RESET}"
        print(f"{idx} {symbol_score} {symbol_1d} {symbol_5m} {symbol_macd} {prefix}")

    print(f"\nCikmak icin CTRL+C")
    sys.stdout.flush()


def main():
    while True:
        candidate_tickers = scanner.fetch_bist_tickers(scanner.TOP_N + scanner.CANDIDATE_BUFFER)
        rows_by_ticker, _still_missing = (
            scanner.fetch_with_retry(candidate_tickers) if candidate_tickers else ({}, [])
        )

        top_rows = sorted(rows_by_ticker.values(), key=lambda r: r["daily"], reverse=True)[: scanner.TOP_N]
        missing_count = max(0, scanner.TOP_N - len(top_rows))

        if top_rows:
            render(top_rows, missing_count=missing_count)
            play_refresh_beep()
            scanner.log_signal_changes(top_rows)
            scanner.log_score_history(top_rows)
        else:
            print("Veri cekilemedi, tekrar denenecek...")
        try:
            time.sleep(scanner.REFRESH_SECONDS)
        except KeyboardInterrupt:
            sys.exit(0)


if __name__ == "__main__":
    main()
