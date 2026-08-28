import csv
import sys
import requests

from datetime import datetime, timezone

from src.delta_strategy import (
    find_kalshi_moves,
    calculate_delta_signal,
    latest_espn_observation,
    edge_to_bet_size,
    select_entry_price,
    MAX_ESPN_ALIGNMENT_SECONDS,
)

from src.espn import (
    find_game,
    get_game_data,
    build_espn_probability_series,
)

from src.historical_kalshi import (
    get_historical_candlesticks,
)


# ============================================================
# CONFIGURATION
# ============================================================

KALSHI_BASE_URL = (
    "https://external-api.kalshi.com/trade-api/v2"
)

SERIES_TICKER = "KXNFLGAME"

START_DATE = datetime(
    2025,
    9,
    1,
    tzinfo=timezone.utc,
)

END_DATE = datetime(
    2026,
    2,
    8,
    23,
    59,
    59,
    tzinfo=timezone.utc,
)

OUTPUT_FILE = (
    "delta_strategy_results_2025_2026_nfl.csv"
)

WINDOW_SECONDS = 5 * 60
MIN_KALSHI_MOVE = 0.10
MIN_DELTA_EDGE = 0.05


# ============================================================
# HELPERS
# ============================================================

def parse_timestamp(value):
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
    return datetime.strptime(
        value,
        "%Y-%m-%d",
    ).replace(
        tzinfo=timezone.utc
    )


def normalize_team(team):
    mapping = {
        "WAS": "WSH",
        "JAC": "JAX",
        "LA": "LAR",
    }

    return mapping.get(
        team.strip().upper(),
        team.strip().upper(),
    )


# ============================================================
# LOAD NFL GAMES DIRECTLY FROM HISTORICAL KALSHI MARKETS
# ============================================================

def load_games_from_kalshi():
    """
    Load the NFL game list directly from Kalshi's historical
    markets endpoint.

    Each NFL event has an event ticker such as:

        KXNFLGAME-25SEP04DALPHI

    and team-specific market tickers such as:

        KXNFLGAME-25SEP04DALPHI-DAL
        KXNFLGAME-25SEP04DALPHI-PHI

    We use the HOME team market ticker as the representative
    market for the game. The binary NO side represents the
    away team, so this preserves the one-market-per-game
    structure used by the previous backtest.
    """

    url = (
        f"{KALSHI_BASE_URL}/historical/markets"
    )

    markets = []
    cursor = None
    page_number = 0

    print(
        "Loading historical NFL markets from Kalshi..."
    )

    while True:

        params = {
            "series_ticker": SERIES_TICKER,
            "limit": 1000,
        }

        if cursor:
            params["cursor"] = cursor

        response = requests.get(
            url,
            params=params,
            timeout=30,
        )

        response.raise_for_status()

        data = response.json()

        page_markets = data.get(
            "markets",
            [],
        )

        markets.extend(
            page_markets
        )

        page_number += 1

        print(
            f"  Page {page_number}: "
            f"{len(page_markets)} markets "
            f"(total {len(markets)})"
        )

        cursor = data.get(
            "cursor"
        )

        if not cursor:
            break

    print(
        f"  Historical Kalshi markets returned: "
        f"{len(markets)}"
    )

    # ========================================================
    # GROUP TEAM MARKETS BY EVENT
    # ========================================================

    events = {}

    for market in markets:

        ticker = market.get(
            "ticker",
            "",
        )

        event_ticker = market.get(
            "event_ticker",
            "",
        )

        if not ticker:
            continue

        if not event_ticker:
            continue

        if not event_ticker.startswith(
            SERIES_TICKER + "-"
        ):
            continue

        events.setdefault(
            event_ticker,
            []
        ).append(
            market
        )

    games = []

    # ========================================================
    # PARSE NFL EVENTS
    # ========================================================

    for event_ticker, event_markets in events.items():

        parts = event_ticker.split("-")

        if len(parts) != 2:
            continue

        game_code = parts[1]

        # ----------------------------------------------------
        # NFL EVENT FORMAT:
        #
        # 25SEP04DALPHI
        #
        # 25SEP04 = date
        # DAL     = away
        # PHI     = home
        #
        # Total = 13 characters
        # ----------------------------------------------------

        if len(game_code) != 13:
            continue

        date_code = game_code[:7]
        away_team = game_code[7:10]
        home_team = game_code[10:13]

        try:

            game_date = datetime.strptime(
                date_code,
                "%y%b%d",
            ).replace(
                tzinfo=timezone.utc
            )

        except ValueError:

            continue

        # ----------------------------------------------------
        # DATE FILTER
        # ----------------------------------------------------

        if game_date < START_DATE:
            continue

        if game_date > END_DATE:
            continue

        # ----------------------------------------------------
        # FIND HOME-TEAM MARKET
        #
        # This is the market we use as the representative
        # binary market for the game.
        # ----------------------------------------------------

        home_ticker = None

        for market in event_markets:

            ticker = market.get(
                "ticker",
                "",
            )

            suffix = ticker.rsplit(
                "-",
                1,
            )[-1]

            if suffix == home_team:

                home_ticker = ticker
                break

        if home_ticker is None:
            continue

        games.append({

            "date":
                game_date.strftime(
                    "%Y-%m-%d"
                ),

            "away_team":
                away_team,

            "home_team":
                home_team,

            "ticker":
                home_ticker,

            "event_ticker":
                event_ticker,
        })

    # ========================================================
    # DEDUPLICATE GAMES
    # ========================================================

    unique_games = {}

    for game in games:

        key = (
            game["date"],
            game["away_team"],
            game["home_team"],
        )

        unique_games[key] = game

    games = list(
        unique_games.values()
    )

    games.sort(
        key=lambda game: (
            game["date"],
            game["away_team"],
            game["home_team"],
        )
    )

    print(
        f"  NFL games in requested range: "
        f"{len(games)}"
    )

    return games


# ============================================================
# WORKING SIGNAL GENERATION
# ============================================================

def inspect_game(
    game_kickoff,
    game_data,
    kalshi_candles,
    kalshi_yes_side,
):
    """
    Generate valid delta signals for one game.

    This intentionally uses the known-working ESPN path:

        src.espn.build_espn_probability_series()

    rather than generate_delta_signals() from delta_strategy.py.

    We do NOT modify the strategy's underlying signal logic.
    """

    # --------------------------------------------------------
    # ESPN SERIES
    # --------------------------------------------------------

    espn_series = build_espn_probability_series(
        game_data,
        game_kickoff,
    )

    if not espn_series:
        return []

    # --------------------------------------------------------
    # KALSHI MOVES
    # --------------------------------------------------------

    moves = find_kalshi_moves(
        kalshi_candles,
        window_seconds=WINDOW_SECONDS,
        min_move=MIN_KALSHI_MOVE,
    )

    if not moves:
        return []

    signals = []

    used_end_plays = set()

    # --------------------------------------------------------
    # EACH QUALIFYING KALSHI MOVEMENT
    # --------------------------------------------------------

    for move in moves:

        start_kalshi = move["start"]
        end_kalshi = move["end"]

        # ----------------------------------------------------
        # START ESPN
        # ----------------------------------------------------

        start_espn = latest_espn_observation(
            espn_series,
            start_kalshi["timestamp"],
            max_alignment_seconds=MAX_ESPN_ALIGNMENT_SECONDS,
        )

        if start_espn is None:
            continue

        # ----------------------------------------------------
        # END ESPN
        # ----------------------------------------------------

        end_espn = latest_espn_observation(
            espn_series,
            end_kalshi["timestamp"],
            max_alignment_seconds=MAX_ESPN_ALIGNMENT_SECONDS,
        )

        if end_espn is None:
            continue

        # ----------------------------------------------------
        # REQUIRE DIFFERENT ESPN PLAYS
        # ----------------------------------------------------

        start_play_id = start_espn.get(
            "play_id"
        )

        end_play_id = end_espn.get(
            "play_id"
        )

        if (
            start_play_id is not None
            and end_play_id is not None
            and start_play_id == end_play_id
        ):
            continue

        # ----------------------------------------------------
        # REQUIRE ESPN TIME TO ADVANCE
        # ----------------------------------------------------

        if (
            start_play_id is None
            or end_play_id is None
        ):

            if (
                start_espn["timestamp"]
                >= end_espn["timestamp"]
            ):
                continue

        # ----------------------------------------------------
        # ONE SIGNAL PER ESPN END PLAY
        # ----------------------------------------------------

        if end_play_id is not None:

            if end_play_id in used_end_plays:
                continue

            used_end_plays.add(
                end_play_id
            )

        # ----------------------------------------------------
        # DELTA CALCULATION
        # ----------------------------------------------------

        if kalshi_yes_side == "home":

            espn_delta = (
                end_espn["home_probability"]
                - start_espn["home_probability"]
            )

        else:

            espn_delta = (
                end_espn["away_probability"]
                - start_espn["away_probability"]
            )

        kalshi_delta = (
            end_kalshi["yes_ask"]
            - start_kalshi["yes_ask"]
        )

        discrepancy = (
            espn_delta
            - kalshi_delta
        )

        edge = abs(
            discrepancy
        )

        # ----------------------------------------------------
        # EDGE REQUIREMENT
        # ----------------------------------------------------

        if edge <= MIN_DELTA_EDGE:
            continue

        # ----------------------------------------------------
        # FINAL SIGNAL
        # ----------------------------------------------------

        signal = calculate_delta_signal(
            start_kalshi=start_kalshi,
            end_kalshi=end_kalshi,
            start_espn=start_espn,
            end_espn=end_espn,
            kalshi_yes_side=kalshi_yes_side,
            min_delta_edge=MIN_DELTA_EDGE,
        )

        if signal is None:
            continue

        signal["kalshi_move"] = (
            move["absolute_kalshi_move"]
        )

        signal["espn_timestamp_source"] = (
            "ESPN_WALLCLOCK"
        )

        signals.append(
            signal
        )

    return signals


# ============================================================
# GAME OUTCOME
# ============================================================

def get_winner(game):
    """
    Return 'home' or 'away' based on the ESPN game result.
    """

    home_winner = game.get(
        "home_winner"
    )

    if home_winner is True:
        return "home"

    if home_winner is False:

        home_score = game.get(
            "home_score"
        )

        away_score = game.get(
            "away_score"
        )

        if (
            home_score is not None
            and away_score is not None
        ):

            if away_score > home_score:
                return "away"

    home_score = game.get(
        "home_score"
    )

    away_score = game.get(
        "away_score"
    )

    if (
        home_score is not None
        and away_score is not None
    ):

        if home_score > away_score:
            return "home"

        if away_score > home_score:
            return "away"

    return None


# ============================================================
# TRADE P&L
# ============================================================

def calculate_trade_result(
    signal,
    winner,
):
    """
    Calculate binary-contract P&L before fees.

    Stake = dynamic $10 / $15 / $20.

    Entry price is the price of the side actually traded.

    Number of contracts:

        stake / entry_price

    If win:

        payout = contracts
        P&L = payout - stake

    If loss:

        P&L = -stake
    """

    edge = signal["edge"]

    stake = edge_to_bet_size(
        edge
    )

    if stake <= 0:
        return None

    traded_side = signal[
        "traded_side"
    ]

    entry_price = select_entry_price(
        signal
    )

    if entry_price <= 0:
        return None

    if entry_price > 1:
        return None

    contracts = (
        stake / entry_price
    )

    won = (
        traded_side == winner
    )

    if won:

        payout = contracts

        pnl = (
            payout
            - stake
        )

    else:

        payout = 0.0

        pnl = -stake

    return {
        "stake": stake,
        "entry_price": entry_price,
        "contracts": contracts,
        "payout": payout,
        "pnl": pnl,
        "won": won,
    }


# ============================================================
# PRINT TRADE
# ============================================================

def print_trade(
    trade_number,
    trade,
):
    print()
    print("-" * 120)

    print(
        f"TRADE #{trade_number}"
    )

    print(
        f"{trade['away_team']} @ "
        f"{trade['home_team']} "
        f"({trade['date']})"
    )

    print(
        f"ESPN game: {trade['game_id']}"
    )

    print()

    print(
        f"Interval: "
        f"{trade['interval_start']} -> "
        f"{trade['interval_end']}"
    )

    print(
        f"Kalshi YES: "
        f"{trade['kalshi_start']:.4f} -> "
        f"{trade['kalshi_end']:.4f}"
    )

    print(
        f"Kalshi move: "
        f"{trade['kalshi_move']:+.4f}"
    )

    print(
        f"ESPN YES delta: "
        f"{trade['espn_yes_delta']:+.4f}"
    )

    print(
        f"Discrepancy / edge: "
        f"{trade['delta_discrepancy']:+.4f} / "
        f"{trade['edge']:.4f}"
    )

    print()

    print(
        f"Trade: "
        f"{trade['traded_side'].upper()}"
    )

    print(
        f"Entry price: "
        f"{trade['entry_price']:.4f}"
    )

    print(
        f"Bet size: "
        f"${trade['stake']:.2f}"
    )

    print(
        f"Contracts: "
        f"{trade['contracts']:.4f}"
    )

    print(
        f"Winner: "
        f"{trade['winner'].upper()}"
    )

    print(
        f"Result: "
        f"{'WIN' if trade['won'] else 'LOSS'}"
    )

    print(
        f"P&L: "
        f"${trade['pnl']:+.2f}"
    )

    print("-" * 120)


# ============================================================
# WRITE RESULTS CSV
# ============================================================

def write_results_csv(
    trades,
    path,
):
    """
    Write every completed trade to the Delta Strategy
    results CSV.
    """

    if not trades:

        print(
            f"No trades to write to {path}."
        )

        return

    fieldnames = [
        "game_id",
        "date",
        "away_team",
        "home_team",
        "winner",
        "traded_side",
        "interval_start",
        "interval_end",
        "kalshi_start",
        "kalshi_end",
        "kalshi_move",
        "espn_yes_delta",
        "delta_discrepancy",
        "edge",
        "entry_price",
        "stake",
        "contracts",
        "payout",
        "won",
        "pnl",
    ]

    with open(
        path,
        "w",
        newline="",
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=fieldnames,
        )

        writer.writeheader()

        writer.writerows(
            trades
        )

    print()
    print(
        f"Wrote {len(trades)} trades to "
        f"{path}"
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 120)
    print("DELTA STRATEGY — FULL BACKTEST")
    print("=" * 120)

    print()
    print("Strategy:")

    print(
        f"  Kalshi movement: >= "
        f"{MIN_KALSHI_MOVE:.0%} "
        f"over exactly "
        f"{WINDOW_SECONDS // 60} minutes"
    )

    print(
        f"  Delta edge: > "
        f"{MIN_DELTA_EDGE:.0%}"
    )

    print(
        f"  ESPN alignment: <= "
        f"{MAX_ESPN_ALIGNMENT_SECONDS} seconds"
    )

    print(
        "  Bet sizing: dynamic ($10 / $15 / $20)"
    )

    print(
        "  Entry: Kalshi price at end of movement"
    )

    print(
        "  P&L: binary contract, before fees"
    )

    print()

    print(
        f"Kalshi series: "
        f"{SERIES_TICKER}"
    )

    print(
        f"Date range: "
        f"{START_DATE.date()} -> "
        f"{END_DATE.date()}"
    )

    print()

    # ========================================================
    # LOAD GAME LIST DIRECTLY FROM KALSHI
    # ========================================================

    try:

        rows = load_games_from_kalshi()

    except requests.RequestException as e:

        print(
            "Could not load historical NFL markets "
            "from Kalshi:"
        )

        print(
            f"  {type(e).__name__}: {e}"
        )

        sys.exit(1)

    except Exception as e:

        print(
            "Could not construct NFL game list:"
        )

        print(
            f"  {type(e).__name__}: {e}"
        )

        sys.exit(1)

    if not rows:

        print(
            "No NFL games found."
        )

        sys.exit(1)

    print(
        f"Loaded {len(rows)} NFL games."
    )

    print()

    # ========================================================
    # AGGREGATE STATISTICS
    # ========================================================

    games_processed = 0
    games_with_trades = 0

    trades = []

    total_staked = 0.0
    total_pnl = 0.0

    wins = 0
    losses = 0

    home_trades = 0
    away_trades = 0

    home_wins = 0
    away_wins = 0

    home_pnl = 0.0
    away_pnl = 0.0

    edge_sum = 0.0

    # ========================================================
    # PROCESS GAMES
    # ========================================================

    for row_number, row in enumerate(
        rows,
        1,
    ):

        away_team = (
            row.get("away_team")
            or row.get("away")
        )

        home_team = (
            row.get("home_team")
            or row.get("home")
        )

        try:

            date = (
                row.get("date")
                or row.get("game_date")
            )

            ticker = (
                row.get("ticker")
                or row.get("kalshi_ticker")
            )

            if not all([
                date,
                away_team,
                home_team,
                ticker,
            ]):
                continue

            games_processed += 1

            print(
                f"[{row_number}/{len(rows)}] "
                f"{away_team} @ {home_team} "
                f"({date})"
            )

            # ------------------------------------------------
            # ESPN
            # ------------------------------------------------

            game = find_game(
                home_team=home_team,
                away_team=away_team,
                game_date=parse_date(
                    date
                ),
            )

            kickoff = game[
                "start_time"
            ]

            game_data = get_game_data(
                game["id"]
            )

            # ------------------------------------------------
            # GAME WINNER
            # ------------------------------------------------

            winner = get_winner(
                game
            )

            if winner is None:

                print(
                    "  Could not determine game winner."
                )

                continue

            # ------------------------------------------------
            # KALSHI YES SIDE
            # ------------------------------------------------

            yes_team = ticker.split(
                "-"
            )[-1]

            normalized_yes = normalize_team(
                yes_team
            )

            normalized_home = normalize_team(
                home_team
            )

            normalized_away = normalize_team(
                away_team
            )

            if normalized_yes == normalized_home:

                kalshi_yes_side = "home"

            elif normalized_yes == normalized_away:

                kalshi_yes_side = "away"

            else:

                raise ValueError(
                    f"Could not determine Kalshi YES side: "
                    f"{ticker=}, "
                    f"{away_team=}, "
                    f"{home_team=}"
                )

            # ------------------------------------------------
            # KALSHI CANDLES
            # ------------------------------------------------

            kickoff_ts = parse_timestamp(
                kickoff
            )

            if kickoff_ts is None:

                raise ValueError(
                    f"Could not parse kickoff: "
                    f"{kickoff}"
                )

            candles = get_historical_candlesticks(
                ticker=ticker,
                start_ts=kickoff_ts - 15 * 60,
                end_ts=kickoff_ts + 4 * 60 * 60,
                period_interval=1,
            )

            if not candles:

                print(
                    "  No Kalshi candles."
                )

                continue

            # ------------------------------------------------
            # FIND VALID SIGNALS
            # ------------------------------------------------

            signals = inspect_game(
                game_kickoff=kickoff,
                game_data=game_data,
                kalshi_candles=candles,
                kalshi_yes_side=kalshi_yes_side,
            )

            if not signals:

                print(
                    "  No trades."
                )

                continue

            games_with_trades += 1

            print(
                f"  {len(signals)} trade(s)"
            )

            # ------------------------------------------------
            # PROCESS EACH TRADE
            # ------------------------------------------------

            for signal in signals:

                result = calculate_trade_result(
                    signal=signal,
                    winner=winner,
                )

                if result is None:
                    continue

                trade = {

                    "game_id":
                        game["id"],

                    "date":
                        date,

                    "away_team":
                        away_team,

                    "home_team":
                        home_team,

                    "winner":
                        winner,

                    "traded_side":
                        signal["traded_side"],

                    "interval_start":
                        signal[
                            "interval_start"
                        ].isoformat(),

                    "interval_end":
                        signal[
                            "interval_end"
                        ].isoformat(),

                    "kalshi_start":
                        signal[
                            "kalshi_start"
                        ],

                    "kalshi_end":
                        signal[
                            "kalshi_end"
                        ],

                    "kalshi_move":
                        signal[
                            "kalshi_move"
                        ],

                    "espn_yes_delta":
                        signal[
                            "espn_yes_delta"
                        ],

                    "delta_discrepancy":
                        signal[
                            "delta_discrepancy"
                        ],

                    "edge":
                        signal[
                            "edge"
                        ],

                    "entry_price":
                        result[
                            "entry_price"
                        ],

                    "stake":
                        result[
                            "stake"
                        ],

                    "contracts":
                        result[
                            "contracts"
                        ],

                    "payout":
                        result[
                            "payout"
                        ],

                    "won":
                        result[
                            "won"
                        ],

                    "pnl":
                        result[
                            "pnl"
                        ],
                }

                trades.append(
                    trade
                )

                # ------------------------------------------------
                # AGGREGATES
                # ------------------------------------------------

                total_staked += (
                    result["stake"]
                )

                total_pnl += (
                    result["pnl"]
                )

                edge_sum += (
                    signal["edge"]
                )

                if result["won"]:

                    wins += 1

                else:

                    losses += 1

                if signal["traded_side"] == "home":

                    home_trades += 1
                    home_pnl += result["pnl"]

                    if result["won"]:
                        home_wins += 1

                else:

                    away_trades += 1
                    away_pnl += result["pnl"]

                    if result["won"]:
                        away_wins += 1

                print_trade(
                    len(trades),
                    trade,
                )

        except KeyboardInterrupt:

            print()
            print(
                "Stopped by user."
            )

            break

        except Exception as e:

            print(
                f"  ERROR: "
                f"{type(e).__name__}: {e}"
            )

    # ========================================================
    # WRITE TRADE RESULTS
    # ========================================================

    write_results_csv(
        trades,
        OUTPUT_FILE,
    )

    # ========================================================
    # FINAL SUMMARY
    # ========================================================

    print()
    print("=" * 120)
    print("DELTA STRATEGY — FULL BACKTEST SUMMARY")
    print("=" * 120)

    print()

    print(
        f"Games processed:       "
        f"{games_processed}"
    )

    print(
        f"Games with trades:     "
        f"{games_with_trades}"
    )

    print(
        f"Trades:                "
        f"{len(trades)}"
    )

    print()

    print(
        f"Wins:                  "
        f"{wins}"
    )

    print(
        f"Losses:                "
        f"{losses}"
    )

    if trades:

        win_rate = (
            wins / len(trades)
        )

        average_edge = (
            edge_sum / len(trades)
        )

    else:

        win_rate = 0.0
        average_edge = 0.0

    print(
        f"Win rate:              "
        f"{win_rate:.2%}"
    )

    print()

    print(
        f"Total staked:          "
        f"${total_staked:.2f}"
    )

    print(
        f"Total P&L:             "
        f"${total_pnl:+.2f}"
    )

    if total_staked > 0:

        roi = (
            total_pnl
            / total_staked
        )

    else:

        roi = 0.0

    print(
        f"ROI:                   "
        f"{roi:+.2%}"
    )

    print(
        f"Average edge:          "
        f"{average_edge:.2%}"
    )

    if trades:

        average_pnl = (
            total_pnl
            / len(trades)
        )

    else:

        average_pnl = 0.0

    print(
        f"Average trade P&L:     "
        f"${average_pnl:+.2f}"
    )

    print()

    # ========================================================
    # HOME / AWAY
    # ========================================================

    print("BY TRADED SIDE")
    print("-" * 120)

    print(
        f"HOME: "
        f"{home_trades} trades | "
        f"{home_wins} wins | "
        f"{home_trades - home_wins} losses | "
        f"P&L ${home_pnl:+.2f}"
    )

    if home_trades:

        print(
            f"      Win rate: "
            f"{home_wins / home_trades:.2%}"
        )

    print(
        f"AWAY: "
        f"{away_trades} trades | "
        f"{away_wins} wins | "
        f"{away_trades - away_wins} losses | "
        f"P&L ${away_pnl:+.2f}"
    )

    if away_trades:

        print(
            f"      Win rate: "
            f"{away_wins / away_trades:.2%}"
        )

    print()

    # ========================================================
    # BET SIZE BREAKDOWN
    # ========================================================

    print("BY BET SIZE")
    print("-" * 120)

    for size in [
        10.0,
        15.0,
        20.0,
    ]:

        size_trades = [
            trade
            for trade in trades
            if trade["stake"] == size
        ]

        size_wins = sum(
            trade["won"]
            for trade in size_trades
        )

        size_pnl = sum(
            trade["pnl"]
            for trade in size_trades
        )

        size_staked = sum(
            trade["stake"]
            for trade in size_trades
        )

        print(
            f"${size:.0f}: "
            f"{len(size_trades)} trades | "
            f"{size_wins} wins | "
            f"{len(size_trades) - size_wins} losses | "
            f"staked ${size_staked:.2f} | "
            f"P&L ${size_pnl:+.2f}"
        )

    print()

    print("=" * 120)
    print("BACKTEST COMPLETE")
    print("=" * 120)


if __name__ == "__main__":
    main()