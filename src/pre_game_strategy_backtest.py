"""
NFL backtest for the Kalshi / ESPN trading strategy.

Backtests NFL games from Week 1 through Super Bowl LX.

For each game:
    1. Find the ESPN game and pregame win probability.
    2. Look at Kalshi 5 minutes before kickoff.
    3. Calculate ESPN probability - Kalshi YES ask.
    4. Use src.strategy.calculate_bet_size(edge).
    5. Buy the side with the largest positive edge.
    6. Calculate the resulting P&L.
"""

import csv
import re
import time
from collections import defaultdict
from datetime import datetime, timezone

import requests

from src.espn import find_game
from src.historical_kalshi import get_ask_at_time
from src.pre_game_strategy import calculate_bet_size


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

QUOTE_OFFSET_SECONDS = 0

ANALYSIS_THRESHOLDS = [
    0.05,
    0.06,
    0.07,
    0.08,
    0.09,
    0.10,
    0.12,
    0.15,
]

EDGE_BUCKETS = [
    (0.05, 0.06),
    (0.06, 0.07),
    (0.07, 0.08),
    (0.08, 0.09),
    (0.09, 0.10),
    (0.10, 0.12),
    (0.12, 0.15),
    (0.15, float("inf")),
]

HEADERS = {
    "User-Agent": "curl/8.0",
    "Accept": "application/json",
}

REQUEST_TIMEOUT = 15

OUTPUT_FILE = "backtest_results_2025_2026_nfl.csv"
SKIPPED_FILE = "backtest_skipped_2025_2026_nfl.csv"


# ============================================================
# HTTP
# ============================================================

session = requests.Session()
session.headers.update(HEADERS)


def get_json(url, params=None, retries=3):
    """GET JSON with simple retry handling."""

    last_error = None

    for attempt in range(retries):

        try:
            response = session.get(
                url,
                params=params,
                timeout=REQUEST_TIMEOUT,
            )

            if response.status_code == 429:
                wait = 2 ** attempt

                print(
                    f"    Kalshi rate limit; "
                    f"waiting {wait}s..."
                )

                time.sleep(wait)
                continue

            response.raise_for_status()

            return response.json()

        except requests.RequestException as exc:

            last_error = exc

            if attempt < retries - 1:
                time.sleep(1)

    raise last_error


# ============================================================
# HISTORICAL MARKETS
# ============================================================

def get_historical_markets():
    """
    Download all historical KXNFLGAME markets.

    Uses cursor pagination so that the complete historical
    series is retrieved.
    """

    url = (
        f"{KALSHI_BASE_URL}/historical/markets"
    )

    markets = []
    cursor = None
    page = 0

    print(
        "Downloading historical KXNFLGAME markets..."
    )

    while True:

        params = {
            "series_ticker": SERIES_TICKER,
            "limit": 1000,
        }

        if cursor:
            params["cursor"] = cursor

        data = get_json(
            url,
            params=params,
        )

        page_markets = data.get(
            "markets",
            [],
        )

        markets.extend(page_markets)

        page += 1

        print(
            f"  Page {page}: "
            f"{len(page_markets)} markets "
            f"(total {len(markets)})"
        )

        cursor = data.get("cursor")

        if not cursor:
            break

    return markets


# ============================================================
# NFL GAME IDENTIFICATION
# ============================================================

NFL_EVENT_PATTERN = re.compile(
    r"^KXNFLGAME-"
    r"(?P<year>\d{2})"
    r"(?P<month>[A-Z]{3})"
    r"(?P<day>\d{2})"
    r"(?P<away>[A-Z0-9]{3})"
    r"(?P<home>[A-Z0-9]{3})$"
)


def parse_event_ticker(event_ticker):
    """Parse a KXNFLGAME event ticker."""

    match = NFL_EVENT_PATTERN.match(
        event_ticker
    )

    if not match:
        return None

    year = 2000 + int(
        match.group("year")
    )

    month = datetime.strptime(
        match.group("month"),
        "%b",
    ).month

    day = int(
        match.group("day")
    )

    game_date = datetime(
        year,
        month,
        day,
        tzinfo=timezone.utc,
    )

    if (
        game_date < START_DATE
        or game_date > END_DATE
    ):
        return None

    return {
        "event_ticker": event_ticker,
        "away_team": match.group("away"),
        "home_team": match.group("home"),
        "date": game_date,
    }


def find_nfl_games(markets):
    """
    Convert historical Kalshi markets into unique NFL games.
    """

    grouped = defaultdict(list)

    for market in markets:

        ticker = market.get(
            "ticker",
            "",
        )

        event_ticker = market.get(
            "event_ticker",
            "",
        )

        if not ticker.startswith(
            SERIES_TICKER + "-"
        ):
            continue

        parsed = parse_event_ticker(
            event_ticker
        )

        if parsed is None:
            continue

        grouped[event_ticker].append(
            market
        )

    games = []

    for event_ticker, event_markets in grouped.items():

        parsed = parse_event_ticker(
            event_ticker
        )

        if parsed is None:
            continue

        team_markets = {}

        for market in event_markets:

            ticker = market.get(
                "ticker",
                "",
            )

            if "-" not in ticker:
                continue

            suffix = ticker.rsplit(
                "-",
                1,
            )[-1]

            if suffix in (
                parsed["home_team"],
                parsed["away_team"],
            ):
                team_markets[suffix] = market

        if len(team_markets) < 2:
            continue

        games.append({
            "event_ticker": event_ticker,
            "away_team": parsed["away_team"],
            "home_team": parsed["home_team"],
            "date": parsed["date"],
            "markets": team_markets,
        })

    games.sort(
        key=lambda game: (
            game["date"],
            game["event_ticker"],
        )
    )

    return games


# ============================================================
# P&L
# ============================================================

def calculate_pnl(
    bet_size,
    yes_ask,
    won,
):
    """Calculate P&L for a YES position."""

    if not won:
        return -bet_size

    contracts = bet_size / yes_ask

    return contracts * (
        1.0 - yes_ask
    )


# ============================================================
# SINGLE GAME
# ============================================================

def backtest_game(game):
    """Backtest one NFL game."""

    away_team = game["away_team"]
    home_team = game["home_team"]
    game_date = game["date"]

    # --------------------------------------------------------
    # ESPN
    # --------------------------------------------------------

    espn_game = find_game(
        home_team,
        away_team,
        game_date,
    )

    start_time = espn_game["start_time"]

    kickoff = datetime.fromisoformat(
        start_time.replace(
            "Z",
            "+00:00",
        )
    )

    kickoff_ts = int(
        kickoff.timestamp()
    )

    quote_ts = (
        kickoff_ts
        - QUOTE_OFFSET_SECONDS
    )

    # --------------------------------------------------------
    # Kalshi quotes
    # --------------------------------------------------------

    quotes = {}

    for team in (
        away_team,
        home_team,
    ):

        market = game["markets"].get(
            team
        )

        if market is None:
            continue

        ticker = market["ticker"]

        yes_ask = get_ask_at_time(
            ticker,
            quote_ts,
        )

        if yes_ask is None:
            continue

        quotes[team] = {
            "ticker": ticker,
            "yes_ask": yes_ask,
        }

    if not quotes:
        raise ValueError(
            "No usable Kalshi quotes at "
            f"{datetime.fromtimestamp(quote_ts, timezone.utc)}"
        )

    # --------------------------------------------------------
    # ESPN probabilities
    # --------------------------------------------------------

    probabilities = {
        away_team: espn_game[
            "away_probability"
        ],
        home_team: espn_game[
            "home_probability"
        ],
    }

    # --------------------------------------------------------
    # Calculate edges
    # --------------------------------------------------------

    candidates = []

    for team, quote in quotes.items():

        probability = probabilities[team]
        ask = quote["yes_ask"]

        edge = probability - ask

        candidates.append({
            "team": team,
            "espn_probability": probability,
            "kalshi_yes_ask": ask,
            "edge": edge,
            "ticker": quote["ticker"],
        })

    if not candidates:
        raise ValueError(
            "No usable trade candidates"
        )

    # Largest positive edge.
    best = max(
        candidates,
        key=lambda x: x["edge"],
    )

    bet_size = calculate_bet_size(
        best["edge"]
    )

    # --------------------------------------------------------
    # Determine winner
    # --------------------------------------------------------

    if espn_game["home_winner"] is None:
        raise ValueError(
            "ESPN result unavailable"
        )

    if espn_game["home_winner"]:
        winning_team = home_team
    else:
        winning_team = away_team

    traded = bet_size > 0

    pnl = 0.0

    if traded:
        pnl = calculate_pnl(
            bet_size,
            best["kalshi_yes_ask"],
            best["team"] == winning_team,
        )

    return {
        "date": game_date.strftime(
            "%Y-%m-%d"
        ),
        "away_team": away_team,
        "home_team": home_team,
        "espn_game_id": espn_game["id"],
        "kickoff": start_time,
        "quote_time": datetime.fromtimestamp(
            quote_ts,
            timezone.utc,
        ).isoformat(),
        "traded_team": (
            best["team"]
            if traded
            else ""
        ),
        "espn_probability": best[
            "espn_probability"
        ],
        "kalshi_yes_ask": best[
            "kalshi_yes_ask"
        ],
        "edge": best["edge"],
        "bet_size": bet_size,
        "winning_team": winning_team,
        "won": (
            best["team"] == winning_team
            if traded
            else None
        ),
        "pnl": pnl,
        "side": (
            "HOME"
            if best["team"] == home_team
            else "AWAY"
        ),
        "ticker": best["ticker"],
    }


# ============================================================
# ANALYSIS
# ============================================================

def summarize_results(results):
    """Print overall backtest summary."""

    trades = [
        r
        for r in results
        if r["bet_size"] > 0
    ]

    wins = [
        r
        for r in trades
        if r["won"]
    ]

    losses = [
        r
        for r in trades
        if not r["won"]
    ]

    total_staked = sum(
        r["bet_size"]
        for r in trades
    )

    total_pnl = sum(
        r["pnl"]
        for r in trades
    )

    roi = (
        total_pnl / total_staked
        if total_staked > 0
        else 0
    )

    avg_edge = (
        sum(r["edge"] for r in trades)
        / len(trades)
        if trades
        else 0
    )

    avg_pnl = (
        total_pnl / len(trades)
        if trades
        else 0
    )

    print()
    print("=" * 60)
    print("BACKTEST SUMMARY")
    print("=" * 60)

    print(
        f"Games successfully tested: "
        f"{len(results)}"
    )

    print(
        f"Trades: {len(trades)}"
    )

    print(
        f"Wins: {len(wins)}"
    )

    print(
        f"Losses: {len(losses)}"
    )

    print(
        f"Win rate: "
        f"{len(wins) / len(trades):.2%}"
        if trades
        else "Win rate: 0.00%"
    )

    print(
        f"Total staked: "
        f"${total_staked:.2f}"
    )

    print(
        f"Total P&L: "
        f"${total_pnl:+.2f}"
    )

    print(
        f"ROI: "
        f"{roi:+.2%}"
    )

    print(
        f"Average edge: "
        f"{avg_edge:.2%}"
    )

    print(
        f"Average trade P&L: "
        f"${avg_pnl:+.2f}"
    )

    for side in ("HOME", "AWAY"):

        side_trades = [
            r
            for r in trades
            if r["side"] == side
        ]

        side_wins = [
            r
            for r in side_trades
            if r["won"]
        ]

        side_pnl = sum(
            r["pnl"]
            for r in side_trades
        )

        print(
            f"{side}: "
            f"{len(side_trades)} trades | "
            f"{len(side_wins)} wins | "
            f"P&L ${side_pnl:+.2f}"
        )


def calculate_subset_stats(results):
    """Return statistics for a subset of trades."""

    trades = [
        r
        for r in results
        if r["bet_size"] > 0
    ]

    if not trades:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0,
            "pnl": 0,
            "roi": 0,
        }

    wins = sum(
        1
        for r in trades
        if r["won"]
    )

    losses = len(trades) - wins

    staked = sum(
        r["bet_size"]
        for r in trades
    )

    pnl = sum(
        r["pnl"]
        for r in trades
    )

    return {
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate": wins / len(trades),
        "pnl": pnl,
        "roi": (
            pnl / staked
            if staked > 0
            else 0
        ),
    }


def print_threshold_analysis(results):
    """Show performance at different minimum-edge thresholds."""

    print()
    print("=" * 60)
    print("EDGE THRESHOLD ANALYSIS")
    print("=" * 60)

    print(
        f"{'Threshold':>10} "
        f"{'Trades':>8} "
        f"{'Wins':>7} "
        f"{'Losses':>8} "
        f"{'Win Rate':>12} "
        f"{'P&L':>12} "
        f"{'ROI':>10}"
    )

    print("-" * 75)

    for threshold in ANALYSIS_THRESHOLDS:

        filtered = [
            r
            for r in results
            if r["edge"] >= threshold
        ]

        stats = calculate_subset_stats(
            filtered
        )

        print(
            f"{threshold:>9.0%} "
            f"{stats['trades']:>8} "
            f"{stats['wins']:>7} "
            f"{stats['losses']:>8} "
            f"{stats['win_rate']:>11.2%} "
            f"${stats['pnl']:>+11.2f} "
            f"{stats['roi']:>9.2%}"
        )


def print_bucket_analysis(results):
    """Show performance by edge bucket."""

    print()
    print("=" * 60)
    print("EDGE BUCKET ANALYSIS")
    print("=" * 60)

    print(
        f"{'Edge':>16} "
        f"{'Trades':>8} "
        f"{'Wins':>7} "
        f"{'Losses':>8} "
        f"{'Win Rate':>12} "
        f"{'P&L':>12} "
        f"{'ROI':>10}"
    )

    print("-" * 80)

    for low, high in EDGE_BUCKETS:

        if high == float("inf"):

            bucket = [
                r
                for r in results
                if r["edge"] >= low
            ]

            label = f"{low:.0%}+"

        else:

            bucket = [
                r
                for r in results
                if low <= r["edge"] < high
            ]

            label = (
                f"{low:.0%} - {high:.0%}"
            )

        stats = calculate_subset_stats(
            bucket
        )

        print(
            f"{label:>16} "
            f"{stats['trades']:>8} "
            f"{stats['wins']:>7} "
            f"{stats['losses']:>8} "
            f"{stats['win_rate']:>11.2%} "
            f"${stats['pnl']:>+11.2f} "
            f"{stats['roi']:>9.2%}"
        )


# ============================================================
# CSV OUTPUT
# ============================================================

def save_results(results):
    """Save backtest results."""

    fieldnames = [
        "date",
        "away_team",
        "home_team",
        "espn_game_id",
        "kickoff",
        "quote_time",
        "traded_team",
        "espn_probability",
        "kalshi_yes_ask",
        "edge",
        "bet_size",
        "winning_team",
        "won",
        "pnl",
        "side",
        "ticker",
    ]

    with open(
        OUTPUT_FILE,
        "w",
        newline="",
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=fieldnames,
        )

        writer.writeheader()

        for result in results:
            writer.writerow(result)


def save_skipped(skipped):
    """Save skipped games and reasons."""

    with open(
        SKIPPED_FILE,
        "w",
        newline="",
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=[
                "date",
                "away_team",
                "home_team",
                "reason",
            ],
        )

        writer.writeheader()

        writer.writerows(skipped)


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 60)
    print("KALSHI NFL FULL-SEASON BACKTEST")
    print("=" * 60)

    print(
        "Period: "
        "2025-09-01 through 2026-02-08"
    )

    print(
        "Scope: NFL Week 1 through Super Bowl LX"
    )

    print(
        "Pregame quote: "
        "5 minutes before kickoff"
    )

    print(
        "Position sizing: "
        "src.strategy.calculate_bet_size()"
    )

    print()
    print("=" * 60)
    print("DOWNLOADING HISTORICAL KALSHI MARKETS")
    print("=" * 60)

    try:
        markets = get_historical_markets()

    except Exception as exc:

        print(
            f"ERROR downloading historical markets: "
            f"{exc}"
        )

        return

    print(
        f"Retrieved {len(markets)} "
        f"historical markets..."
    )

    games = find_nfl_games(markets)

    print(
        f"Historical NFL games found: "
        f"{len(games)}"
    )

    if not games:

        print()
        print(
            "ERROR: 0 NFL games found."
        )

        return

    print()
    print("=" * 60)
    print("RUNNING BACKTEST")
    print("=" * 60)

    results = []
    skipped = []

    for i, game in enumerate(
        games,
        start=1,
    ):

        label = (
            f"{game['away_team']} @ "
            f"{game['home_team']} "
            f"{game['date'].date()}"
        )

        print(
            f"[{i}/{len(games)}] "
            f"{label}",
            end="",
        )

        try:

            result = backtest_game(
                game
            )

            results.append(result)

            if result["bet_size"] > 0:

                outcome = (
                    "WIN"
                    if result["won"]
                    else "LOSS"
                )

                print(
                    f" -> "
                    f"{result['traded_team']} "
                    f"{outcome} "
                    f"${result['pnl']:+.2f}"
                )

            else:

                print(
                    " -> NO TRADE"
                )

        except Exception as exc:

            print(
                f"SKIP: {exc}"
            )

            skipped.append({
                "date": game["date"].strftime(
                    "%Y-%m-%d"
                ),
                "away_team": game["away_team"],
                "home_team": game["home_team"],
                "reason": str(exc),
            })

    summarize_results(
        results
    )

    print_threshold_analysis(
        results
    )

    print_bucket_analysis(
        results
    )

    save_results(
        results
    )

    save_skipped(
        skipped
    )

    print()
    print("=" * 60)
    print("RESULTS SAVED")
    print("=" * 60)

    print(OUTPUT_FILE)

    if skipped:
        print(SKIPPED_FILE)


if __name__ == "__main__":
    main()