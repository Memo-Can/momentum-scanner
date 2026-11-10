"""scanner_bist.py'deki saf (yan etkisiz) fonksiyonlar icin birim
testler - Yahoo/ag/DB erismesi gerektirmeyen mantik (skor esikleri, yuzde
degisim, piyasa saati) hedeflenir. Amac: confluence_label/SCORE_ORDER gibi
CLAUDE.md'de "tutarlilik kontrolu yapilan TUM yerler" diye tek tek elle
tarandigi belirtilen noktalarda, gelecekteki bir esik degisikliginin sessiz
bir regresyona donusmemesi.

Calistirma: cd backend && python3 -m unittest discover -s tests -v
"""

import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import scanner_bist as scanner  # noqa: E402


class ConfluenceLabelBoundaryTests(unittest.TestCase):
    """confluence_label()'in -5..+5 esik siniri - CLAUDE.md'deki "MUTLAK
    AL/SAT etiketleri" bolumunde anlatilan, bilercek SIMETRIK OLMAYAN kurali
    (MUTLAK_AL sadece skor==5, MUTLAK_SAT skor<=-4) dondurur."""

    STRONG_ADX = 25.0  # ADX_TREND_THRESHOLD=20'nin ustunde -> trend_confirmed=True
    WEAK_ADX = 10.0    # trend_confirmed=False

    def label_for_score(self, score, adx):
        # confluence_label kendi ham skorunu confluence_score_raw ile
        # hesapladigi icin, istenen skoru veren bir (signal_1d, signal_macd,
        # rsi, bb_percent, rel_strength) kombinasyonu uretiyoruz. DIKKAT:
        # bb_percent'in (signal_1d/macd/rsi/rel_strength'in aksine) NOTR
        # secenegi YOK - confluence_score_raw'da `+1 if bb_percent>=50 else -1`
        # ile HER ZAMAN +-1 katkisi var, bu yuzden bb ayri ele alinip once
        # sabitleniyor, kalan 4 ternary (-1/0/+1) bilesen onun ARTIGINI
        # tamamliyor.
        bb_contrib = 1 if -3 <= score <= 5 else -1  # sadece score in {-5,-4} icin -1 gerekir
        remaining = score - bb_contrib
        slots = [0, 0, 0, 0]  # signal_1d, signal_macd, rsi, rel_strength
        for i in range(4):
            if remaining > 0:
                slots[i] = 1
                remaining -= 1
            elif remaining < 0:
                slots[i] = -1
                remaining += 1
        signal_1d = "AL" if slots[0] > 0 else ("SAT" if slots[0] < 0 else "NOTR")
        signal_macd = "AL" if slots[1] > 0 else ("SAT" if slots[1] < 0 else "NOTR")
        rsi_val = 60.0 if slots[2] > 0 else (40.0 if slots[2] < 0 else 50.0)
        rel_strength = 1.0 if slots[3] > 0 else (-1.0 if slots[3] < 0 else 0.0)
        bb_percent = 60.0 if bb_contrib > 0 else 40.0
        raw = scanner.confluence_score_raw(signal_1d, signal_macd, rsi_val, bb_percent, rel_strength)
        self.assertEqual(raw, score, "test helper'in uretilen skoru beklenenle uyusmuyor")
        return scanner.confluence_label(signal_1d, signal_macd, rsi_val, bb_percent, adx, rel_strength)

    def test_mutlak_al_requires_full_consensus(self):
        self.assertEqual(self.label_for_score(5, self.STRONG_ADX), "MUTLAK_AL")

    def test_guclu_al_is_exactly_score_four(self):
        self.assertEqual(self.label_for_score(4, self.STRONG_ADX), "GUCLU_AL")

    def test_al_band(self):
        self.assertEqual(self.label_for_score(2, self.STRONG_ADX), "AL")
        self.assertEqual(self.label_for_score(3, self.STRONG_ADX), "AL")

    def test_notr_band(self):
        for score in (-1, 0, 1):
            self.assertEqual(self.label_for_score(score, self.STRONG_ADX), "NOTR")

    def test_sat_band(self):
        self.assertEqual(self.label_for_score(-2, self.STRONG_ADX), "SAT")
        self.assertEqual(self.label_for_score(-3, self.STRONG_ADX), "SAT")

    def test_mutlak_sat_covers_minus_four_and_minus_five(self):
        # Bilerek SIMETRIK DEGIL: GUCLU_SAT canli taramada hic uretilmez,
        # -4 VE -5 direkt MUTLAK_SAT'a gider (bkz. CLAUDE.md).
        self.assertEqual(self.label_for_score(-4, self.STRONG_ADX), "MUTLAK_SAT")
        self.assertEqual(self.label_for_score(-5, self.STRONG_ADX), "MUTLAK_SAT")

    def test_weak_adx_demotes_strong_labels_in_one_step(self):
        # ADX zayifsa MUTLAK_AL/GUCLU_AL -> AL, MUTLAK_SAT/GUCLU_SAT -> SAT
        # (ara kademe YOK, tek adimda dusuyor).
        self.assertEqual(self.label_for_score(5, self.WEAK_ADX), "AL")
        self.assertEqual(self.label_for_score(4, self.WEAK_ADX), "AL")
        self.assertEqual(self.label_for_score(-5, self.WEAK_ADX), "SAT")
        self.assertEqual(self.label_for_score(-4, self.WEAK_ADX), "SAT")

    def test_weak_adx_does_not_affect_already_mild_labels(self):
        self.assertEqual(self.label_for_score(2, self.WEAK_ADX), "AL")
        self.assertEqual(self.label_for_score(-2, self.WEAK_ADX), "SAT")
        self.assertEqual(self.label_for_score(0, self.WEAK_ADX), "NOTR")

    def test_nan_adx_is_treated_as_unconfirmed(self):
        self.assertEqual(self.label_for_score(5, float("nan")), "AL")


class ScoreOrderConsistencyTests(unittest.TestCase):
    """CLAUDE.md: SCORE_ORDER'in frontend'deki DEFAULT_SCORE_ORDER ile
    BIREBIR AYNI tutulmasi gerektigini soyluyor - bu test en azindan backend
    tarafinin kendi icinde tutarli oldugunu (7 etiketin hepsi var, sirali,
    cakisma yok) otomatik dogrular."""

    EXPECTED_LABELS = {"MUTLAK_AL", "GUCLU_AL", "AL", "NOTR", "SAT", "GUCLU_SAT", "MUTLAK_SAT"}

    def test_all_seven_labels_present_with_unique_ranks(self):
        self.assertEqual(set(scanner.SCORE_ORDER.keys()), self.EXPECTED_LABELS)
        ranks = list(scanner.SCORE_ORDER.values())
        self.assertEqual(len(ranks), len(set(ranks)), "SCORE_ORDER'da cakisan rank var")

    def test_rank_order_matches_al_to_sat_direction(self):
        self.assertLess(scanner.SCORE_ORDER["MUTLAK_AL"], scanner.SCORE_ORDER["GUCLU_AL"])
        self.assertLess(scanner.SCORE_ORDER["GUCLU_AL"], scanner.SCORE_ORDER["AL"])
        self.assertLess(scanner.SCORE_ORDER["AL"], scanner.SCORE_ORDER["NOTR"])
        self.assertLess(scanner.SCORE_ORDER["NOTR"], scanner.SCORE_ORDER["SAT"])
        self.assertLess(scanner.SCORE_ORDER["SAT"], scanner.SCORE_ORDER["GUCLU_SAT"])
        self.assertLess(scanner.SCORE_ORDER["GUCLU_SAT"], scanner.SCORE_ORDER["MUTLAK_SAT"])

    def test_strong_buy_zone_is_subset_of_score_order(self):
        for label in scanner._STRONG_BUY_ZONE:
            self.assertIn(label, scanner.SCORE_ORDER)

    def test_strong_signal_labels_cover_both_mutlak_variants(self):
        for label in ("MUTLAK_AL", "GUCLU_AL", "MUTLAK_SAT", "GUCLU_SAT"):
            self.assertIn(label, scanner._STRONG_SIGNAL_LABELS)


class RollingPctChangeTests(unittest.TestCase):
    def test_insufficient_history_returns_nan(self):
        import pandas as pd
        closes = pd.Series([10.0, 11.0, 12.0])
        result = scanner.rolling_pct_change(closes, 13.0, periods=5)
        self.assertNotEqual(result, result)  # NaN != NaN

    def test_normal_window_computes_pct_change(self):
        import pandas as pd
        closes = pd.Series([100.0, 101.0, 102.0, 103.0, 104.0, 105.0])
        # periods=5 -> 5 islem gunu once = closes.iloc[-6] = 100.0
        result = scanner.rolling_pct_change(closes, 110.0, periods=5)
        self.assertAlmostEqual(result, 10.0)

    def test_none_closes_returns_nan(self):
        result = scanner.rolling_pct_change(None, 100.0, periods=5)
        self.assertNotEqual(result, result)


class StopTargetLevelsTests(unittest.TestCase):
    def test_al_direction_stop_below_target_above(self):
        stop, target = scanner.stop_target_levels(100.0, atr_val=2.0, direction="AL", stop_mult=1.5, target_r=3.0)
        self.assertAlmostEqual(stop, 97.0)
        self.assertAlmostEqual(target, 109.0)

    def test_sat_direction_is_mirrored(self):
        stop, target = scanner.stop_target_levels(100.0, atr_val=2.0, direction="SAT", stop_mult=1.5, target_r=3.0)
        self.assertAlmostEqual(stop, 103.0)
        self.assertAlmostEqual(target, 91.0)

    def test_notr_direction_returns_none(self):
        self.assertEqual(scanner.stop_target_levels(100.0, 2.0, "NOTR", 1.5, 3.0), (None, None))

    def test_nan_price_returns_none(self):
        self.assertEqual(scanner.stop_target_levels(float("nan"), 2.0, "AL", 1.5, 3.0), (None, None))


class IsMarketOpenTests(unittest.TestCase):
    def test_weekday_inside_hours_is_open(self):
        # 2026-11-02 Pazartesi, 12:00 - BIST_MARKET_OPEN/CLOSE varsayilan
        # pencere (09:30-18:30) icinde.
        now = datetime.datetime(2026, 11, 2, 12, 0)
        self.assertTrue(scanner.is_market_open(now))

    def test_weekday_before_open_is_closed(self):
        now = datetime.datetime(2026, 11, 2, 8, 0)
        self.assertFalse(scanner.is_market_open(now))

    def test_weekday_after_close_is_closed(self):
        now = datetime.datetime(2026, 11, 2, 19, 0)
        self.assertFalse(scanner.is_market_open(now))

    def test_saturday_is_closed_even_inside_hour_window(self):
        now = datetime.datetime(2026, 11, 7, 12, 0)  # Cumartesi
        self.assertFalse(scanner.is_market_open(now))

    def test_sunday_is_closed(self):
        now = datetime.datetime(2026, 11, 8, 12, 0)  # Pazar
        self.assertFalse(scanner.is_market_open(now))


if __name__ == "__main__":
    unittest.main()
