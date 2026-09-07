import unittest
from datetime import datetime, timedelta, timezone

from src.delta_strategy import generate_delta_signals
from src.paper_delta_trader import (
    live_signal,
    market_pair,
    record_snapshot,
    snapshot_at_window,
)


class PaperDeltaTraderTests(unittest.TestCase):
    def test_market_pair_maps_espn_codes_to_kalshi_tickers(self):
        game = {"away": "WSH", "home": "LAR"}
        markets = [
            {"ticker": "KXNFLGAME-26SEP10WASLAR-WAS", "status": "active"},
            {"ticker": "KXNFLGAME-26SEP10WASLAR-LAR", "status": "active"},
        ]
        self.assertEqual(
            market_pair(markets, game),
            {
                "away": "KXNFLGAME-26SEP10WASLAR-WAS",
                "home": "KXNFLGAME-26SEP10WASLAR-LAR",
            },
        )

    def test_snapshot_requires_an_observation_near_five_minutes_old(self):
        state = {"snapshots": {}}
        now = datetime(2026, 9, 10, 20, 5, tzinfo=timezone.utc)
        record_snapshot(state, "TEST", 0.41, now - timedelta(seconds=300))
        quote = snapshot_at_window(state, "TEST", now)
        self.assertEqual(quote["yes_ask"], 0.41)
        self.assertIsNone(snapshot_at_window(state, "TEST", now + timedelta(seconds=30)))

    def test_live_signal_uses_delta_strategy_thresholds(self):
        now = datetime(2026, 9, 10, 20, 5, tzinfo=timezone.utc)
        game = {"espn_id": "1"}
        start = {"timestamp": now - timedelta(minutes=5), "yes_ask": 0.40}
        end = {"timestamp": now, "yes_ask": 0.52}
        espn = [
            {"timestamp": now - timedelta(minutes=5, seconds=5), "home_probability": 0.40, "away_probability": 0.60, "play_id": "a"},
            {"timestamp": now - timedelta(seconds=5), "home_probability": 0.62, "away_probability": 0.38, "play_id": "b"},
        ]
        signal = live_signal(game, "home", start, end, espn)
        self.assertIsNotNone(signal)
        self.assertEqual(signal["traded_side"], "home")
        self.assertEqual(signal["game_id"], "1")

    def test_strategy_generator_accepts_wallclock_observations(self):
        """Regression: real ESPN wallclock records must not be filtered out."""
        start = datetime(2026, 9, 10, 20, 0, tzinfo=timezone.utc)
        game_data = {"winprobability": [
            {"homeWinPercentage": 0.40, "play": {"id": "a", "wallclock": (start - timedelta(seconds=5)).isoformat()}},
            {"homeWinPercentage": 0.62, "play": {"id": "b", "wallclock": (start + timedelta(minutes=5, seconds=-5)).isoformat()}},
        ]}
        candles = [
            {"end_period_ts": int(start.timestamp()), "yes_ask": 0.40},
            {"end_period_ts": int((start + timedelta(minutes=5)).timestamp()), "yes_ask": 0.52},
        ]
        signals = generate_delta_signals(start, game_data, candles, "home")
        self.assertEqual(len(signals), 1)


if __name__ == "__main__":
    unittest.main()
