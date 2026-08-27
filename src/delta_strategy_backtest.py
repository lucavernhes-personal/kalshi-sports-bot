import csv
import sys
from datetime import datetime, timezone

from src.delta_strategy import (
    generate_delta_signals,
    select_entry_price,
    edge_to_bet_size,
)
from src.espn import find_game, get_game_data
from src.historical_kalshi import get_historical_candlesticks


RESULTS_FILE = "delta_strategy_results_2025_2026_nfl.csv"
INPUT_FILE = "backtest_results_2025_2026_nfl.csv"


# ============================================================
# HELPERS
# ============================================================

def parse_timestamp(value):
    """Parse an ISO timestamp or Unix timestamp into UTC seconds."""

    if value is None or value == "":
        return None

    try:
        return int(float(value))
    except (TypeError, ValueError):
        pass

    value = str(value).replace("Z", "+00:00")

    dt = datetime.fromisoformat(value)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return int(dt.timestamp())


def parse_date(value):
    """Parse YYYY-MM-DD into a UTC datetime."""

    return datetime.strptime(
        value,
        "%Y-%m-%d",
    ).replace(tzinfo=timezone.utc)


def load_games(path):
    """Load historical game rows."""

    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def get_result(traded_side, home_team, away_team, winning_team):
    """
    Return whether the traded side won.

    The input CSV contains the actual winning team, so we use that
    directly rather than relying on a home_winner column.
    """

    if not winning_team:
        raise ValueError(
            "Missing winning_team in input CSV"
        )

    winning_team = winning_team.strip()

    if traded_side == "home":
        traded_team = home_team

    elif traded_side == "away":
        traded_team = away_team

    else:
        raise ValueError(
            f"Unknown traded side: {traded_side}"
        )

    return traded_team == winning_team


def calculate_pnl(ask, bet_size, won):
    """
    Calculate P&L for a YES/NO contract.

    bet_size is the amount risked.

    At price p:

        contracts = bet_size / p

    Winning profit:

        contracts * (1 - p)

    Losing P&L:

        -bet_size
    """

    contracts = bet_size / ask

    if won:
        return contracts * (1.0 - ask)

    return -bet_size


# ============================================================
# MAIN BACKTEST
# ============================================================

def main():

    print("=" * 80)
    print("DELTA STRATEGY BACKTEST")
    print("=" * 80)

    try:
        rows = load_games(INPUT_FILE)

    except FileNotFoundError:

        print()
        print(f"Could not find input file: {INPUT_FILE}")
        print()
        print(
            "Set INPUT_FILE at the top of this script "
            "to your historical game CSV."
        )

        sys.exit(1)

    trades = []

    total_games = 0
    games_with_signals = 0

    for row_number, row in enumerate(rows, 1):

        try:

            # ------------------------------------------------
            # GAME INFORMATION
            # ------------------------------------------------

            date = (
                row.get("date")
                or row.get("game_date")
            )

            away_team = (
                row.get("away_team")
                or row.get("away")
            )

            home_team = (
                row.get("home_team")
                or row.get("home")
            )

            ticker = (
                row.get("ticker")
                or row.get("kalshi_ticker")
            )

            series_ticker = (
                row.get("series_ticker")
                or row.get("kalshi_series")
                or "KXNFLGAME"
            )

            winning_team = (
                row.get("winning_team")
            )

            if not all([
                date,
                away_team,
                home_team,
                ticker,
                winning_team,
            ]):
                continue

            total_games += 1

            game_date = parse_date(date)

            # ------------------------------------------------
            # ESPN GAME
            # ------------------------------------------------

            game = find_game(
                home_team=home_team,
                away_team=away_team,
                game_date=game_date,
            )

            kickoff = game["start_time"]

            game_data = get_game_data(
                game["id"]
            )

            # ------------------------------------------------
            # DETERMINE KALSHI YES SIDE
            # ------------------------------------------------
            #
            # Example:
            #
            # KXNFLGAME-25SEP04DALPHI-DAL
            #
            # means DAL is YES.
            #
            # The final component of the ticker identifies
            # the team represented by YES.

            yes_team = ticker.split("-")[-1]

            if yes_team == home_team:

                kalshi_yes_side = "home"

            elif yes_team == away_team:

                kalshi_yes_side = "away"

            else:

                raise ValueError(
                    f"Could not determine Kalshi YES side: "
                    f"ticker={ticker}, "
                    f"away={away_team}, "
                    f"home={home_team}"
                )

            # ------------------------------------------------
            # KALSHI HISTORICAL DATA
            # ------------------------------------------------

            kickoff_ts = parse_timestamp(
                kickoff
            )

            if kickoff_ts is None:
                raise ValueError(
                    f"Could not parse kickoff timestamp: {kickoff}"
                )

            # Pull the full game window.
            #
            # The delta strategy searches for five-minute
            # movements throughout the game.

            start_ts = kickoff_ts - 15 * 60

            end_ts = kickoff_ts + 4 * 60 * 60

            candles = get_historical_candlesticks(
                ticker=ticker,
                start_ts=start_ts,
                end_ts=end_ts,
                period_interval=1,
            )

            if not candles:

                print(
                    f"[row {row_number}] "
                    f"{away_team} @ {home_team}: "
                    f"no Kalshi candles"
                )

                continue

            # ------------------------------------------------
            # GENERATE STRATEGY SIGNALS
            # ------------------------------------------------

            signals = generate_delta_signals(
                game_kickoff=kickoff,
                game_data=game_data,
                kalshi_candles=candles,
                kalshi_yes_side=kalshi_yes_side,
            )

            if not signals:
                continue

            games_with_signals += 1

            # ------------------------------------------------
            # PROCESS SIGNALS
            # ------------------------------------------------

            for signal in signals:

                traded_side = signal["traded_side"]

                # ------------------------------------------------
                # ENTRY PRICE
                # ------------------------------------------------

                entry_price = select_entry_price(
                    signal
                )

                if (
                    entry_price <= 0
                    or entry_price >= 1
                ):
                    continue

                # ------------------------------------------------
                # BET SIZE
                # ------------------------------------------------

                edge = signal["edge"]

                bet_size = edge_to_bet_size(
                    edge
                )

                if bet_size <= 0:
                    continue

                # ------------------------------------------------
                # RESULT
                # ------------------------------------------------

                won = get_result(
                    traded_side=traded_side,
                    home_team=home_team,
                    away_team=away_team,
                    winning_team=winning_team,
                )

                pnl = calculate_pnl(
                    ask=entry_price,
                    bet_size=bet_size,
                    won=won,
                )

                # ------------------------------------------------
                # SAVE TRADE
                # ------------------------------------------------

                trade = {
                    "date": date,

                    "away_team": away_team,

                    "home_team": home_team,

                    "ticker": ticker,

                    "espn_game_id": game["id"],

                    "event_start": signal[
                        "interval_start"
                    ].isoformat(),

                    "event_end": signal[
                        "interval_end"
                    ].isoformat(),

                    "kalshi_yes_side": signal[
                        "kalshi_yes_side"
                    ],

                    "kalshi_start_price": signal[
                        "kalshi_start"
                    ],

                    "kalshi_end_price": signal[
                        "kalshi_end"
                    ],

                    "kalshi_delta": signal[
                        "kalshi_delta"
                    ],

                    "kalshi_move": signal.get(
                        "kalshi_move"
                    ),

                    "espn_yes_delta": signal[
                        "espn_yes_delta"
                    ],

                    "delta_discrepancy": signal[
                        "delta_discrepancy"
                    ],

                    "edge": edge,

                    "traded_side": traded_side,

                    "traded_team": (
                        home_team
                        if traded_side == "home"
                        else away_team
                    ),

                    "entry_price": entry_price,

                    "bet_size": bet_size,

                    "winning_team": winning_team,

                    "won": won,

                    "pnl": pnl,

                    "espn_start_timestamp": signal[
                        "espn_start_timestamp"
                    ].isoformat(),

                    "espn_end_timestamp": signal[
                        "espn_end_timestamp"
                    ].isoformat(),

                    "espn_home_start": signal[
                        "espn_home_start"
                    ],

                    "espn_home_end": signal[
                        "espn_home_end"
                    ],

                    "espn_away_start": signal[
                        "espn_away_start"
                    ],

                    "espn_away_end": signal[
                        "espn_away_end"
                    ],

                    "espn_start_play_id": signal.get(
                        "espn_start_play_id"
                    ),

                    "espn_end_play_id": signal.get(
                        "espn_end_play_id"
                    ),

                    "espn_start_period": signal.get(
                        "espn_start_period"
                    ),

                    "espn_end_period": signal.get(
                        "espn_end_period"
                    ),

                    "espn_start_clock": signal.get(
                        "espn_start_clock"
                    ),

                    "espn_end_clock": signal.get(
                        "espn_end_clock"
                    ),
                }

                trades.append(trade)

                print(
                    f"[{len(trades):3d}] "
                    f"{away_team} @ {home_team} | "
                    f"{traded_side.upper()} | "
                    f"Kalshi Δ "
                    f"{signal['kalshi_delta']:+.2%} | "
                    f"ESPN Δ "
                    f"{signal['espn_yes_delta']:+.2%} | "
                    f"diff "
                    f"{signal['delta_discrepancy']:+.2%} | "
                    f"entry "
                    f"{entry_price:.2f} | "
                    f"bet "
                    f"${bet_size:.2f} | "
                    f"{'WIN' if won else 'LOSS'} | "
                    f"P&L "
                    f"${pnl:+.2f}"
                )

        except Exception as e:

            print(
                f"[row {row_number}] "
                f"{row.get('away_team', row.get('away', '?'))} @ "
                f"{row.get('home_team', row.get('home', '?'))}: "
                f"{type(e).__name__}: {e}"
            )

    # ============================================================
    # SUMMARY
    # ============================================================

    if not trades:

        print()
        print("No trades generated.")
        print(
            f"Games processed: {total_games}"
        )

        return

    total_staked = sum(
        trade["bet_size"]
        for trade in trades
    )

    total_pnl = sum(
        trade["pnl"]
        for trade in trades
    )

    wins = sum(
        trade["won"]
        for trade in trades
    )

    losses = (
        len(trades)
        - wins
    )

    average_edge = sum(
        trade["edge"]
        for trade in trades
    ) / len(trades)

    # ============================================================
    # SUMMARY
    # ============================================================

    print()
    print("=" * 80)
    print("BACKTEST SUMMARY")
    print("=" * 80)

    print(
        f"Games processed:      {total_games}"
    )

    print(
        f"Games with signals:   {games_with_signals}"
    )

    print(
        f"Trades:               {len(trades)}"
    )

    print(
        f"Wins:                 {wins}"
    )

    print(
        f"Losses:               {losses}"
    )

    print(
        f"Win rate:             "
        f"{wins / len(trades):.2%}"
    )

    print(
        f"Total staked:         "
        f"${total_staked:.2f}"
    )

    print(
        f"Total P&L:            "
        f"${total_pnl:+.2f}"
    )

    print(
        f"ROI:                  "
        f"{total_pnl / total_staked:.2%}"
    )

    print(
        f"Average delta edge:   "
        f"{average_edge:.2%}"
    )

    # ============================================================
    # SIDE BREAKDOWN
    # ============================================================

    print()
    print("=" * 80)
    print("SIDE BREAKDOWN")
    print("=" * 80)

    for side in ("home", "away"):

        side_trades = [
            trade
            for trade in trades
            if trade["traded_side"] == side
        ]

        if not side_trades:
            continue

        side_wins = sum(
            trade["won"]
            for trade in side_trades
        )

        side_staked = sum(
            trade["bet_size"]
            for trade in side_trades
        )

        side_pnl = sum(
            trade["pnl"]
            for trade in side_trades
        )

        print(
            f"{side.upper():5s}: "
            f"{len(side_trades)} trades | "
            f"{side_wins} wins | "
            f"P&L "
            f"${side_pnl:+.2f} | "
            f"ROI "
            f"{side_pnl / side_staked:.2%}"
        )

    # ============================================================
    # EDGE BUCKETS
    # ============================================================

    print()
    print("=" * 80)
    print("EDGE BUCKET ANALYSIS")
    print("=" * 80)

    thresholds = [
        0.05,
        0.06,
        0.07,
        0.08,
        0.09,
        0.10,
        0.12,
        0.15,
    ]

    print(
        f"{'Threshold':>10} "
        f"{'Trades':>8} "
        f"{'Wins':>7} "
        f"{'Losses':>8} "
        f"{'Win Rate':>12} "
        f"{'P&L':>12} "
        f"{'ROI':>10}"
    )

    print("-" * 80)

    for threshold in thresholds:

        bucket = [
            trade
            for trade in trades
            if trade["edge"] >= threshold
        ]

        if not bucket:
            continue

        bucket_staked = sum(
            trade["bet_size"]
            for trade in bucket
        )

        bucket_pnl = sum(
            trade["pnl"]
            for trade in bucket
        )

        bucket_wins = sum(
            trade["won"]
            for trade in bucket
        )

        print(
            f"{threshold:>9.0%} "
            f"{len(bucket):>8d} "
            f"{bucket_wins:>7d} "
            f"{len(bucket) - bucket_wins:>8d} "
            f"{bucket_wins / len(bucket):>11.2%} "
            f"${bucket_pnl:>11.2f} "
            f"{bucket_pnl / bucket_staked:>9.2%}"
        )

    # ============================================================
    # SAVE CSV
    # ============================================================

    with open(
        RESULTS_FILE,
        "w",
        newline="",
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=trades[0].keys(),
        )

        writer.writeheader()
        writer.writerows(trades)

    print()
    print("=" * 80)
    print("RESULTS SAVED")
    print("=" * 80)

    print(RESULTS_FILE)


if __name__ == "__main__":
    main()