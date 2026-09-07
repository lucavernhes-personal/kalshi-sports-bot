"""Run the NFL delta strategy in paper mode.

This module has no authentication and contains no order-submission code.  It
only reads public ESPN/Kalshi data and persists simulated fills to a local JSON
ledger.  A fill is made only once per ESPN end-play and market-side.

Run continuously during games:

    python -m src.paper_delta_trader

Use ``--once`` to verify market discovery and collect a single price snapshot.
"""

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from src.data import get_nfl_markets, get_yes_bid_ask
from src.delta_strategy import (
    MAX_ESPN_ALIGNMENT_SECONDS,
    MIN_DELTA_EDGE,
    MIN_KALSHI_MOVE,
    WINDOW_SECONDS,
    calculate_delta_signal,
    edge_to_bet_size,
    latest_espn_observation,
)
from src.espn import (
    get_game_data,
    get_scoreboard,
    build_espn_probability_series,
)

DEFAULT_STATE_FILE = Path("runtime/paper_delta_state.json")
MAX_SNAPSHOT_LAG_SECONDS = 25


def utc_now():
    return datetime.now(timezone.utc)


def parse_utc(value):
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def kalshi_team(team):
    """Convert ESPN abbreviations to the codes used by KXNFLGAME tickers."""
    return {"WSH": "WAS"}.get(team.upper(), team.upper())


def active_scoreboard_games(now):
    """Return only ESPN NFL games currently in progress, for today and tomorrow."""
    games = []
    seen = set()
    for day_offset in (0, 1):
        date = now.date().fromordinal(now.date().toordinal() + day_offset)
        for event in get_scoreboard(date).get("events", []):
            competition = event.get("competitions", [{}])[0]
            status = competition.get("status", {}).get("type", {})
            if status.get("state") != "in" or event.get("id") in seen:
                continue
            teams = competition.get("competitors", [])
            try:
                home = next(team for team in teams if team["homeAway"] == "home")
                away = next(team for team in teams if team["homeAway"] == "away")
            except (KeyError, StopIteration):
                continue
            seen.add(event["id"])
            games.append({
                "espn_id": event["id"],
                "kickoff": event["date"],
                "home": home["team"]["abbreviation"].upper(),
                "away": away["team"]["abbreviation"].upper(),
            })
    return games


def market_pair(markets, game):
    """Find the active two-sided KXNFLGAME market pair for an ESPN event."""
    away = kalshi_team(game["away"])
    home = kalshi_team(game["home"])
    candidates = {}
    for market in markets:
        ticker = market.get("ticker", "")
        parts = ticker.split("-")
        if len(parts) != 3 or market.get("status") != "active":
            continue
        game_code, team = parts[1], parts[2]
        if game_code.endswith(away + home) and team in {away, home}:
            candidates[team] = ticker
    if set(candidates) != {away, home}:
        return None
    return {"home": candidates[home], "away": candidates[away]}


def load_state(path):
    if not path.exists():
        return {"version": 1, "snapshots": {}, "trades": [], "used_signals": []}
    with path.open() as handle:
        state = json.load(handle)
    state.setdefault("snapshots", {})
    state.setdefault("trades", [])
    state.setdefault("used_signals", [])
    return state


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
    temporary.replace(path)


def record_snapshot(state, ticker, ask, observed_at):
    snapshots = state["snapshots"].setdefault(ticker, [])
    snapshots.append({"timestamp": observed_at.isoformat(), "yes_ask": ask})
    # 30 minutes is ample for a five-minute comparison and bounds the state file.
    cutoff = observed_at.timestamp() - 30 * 60
    state["snapshots"][ticker] = [
        snapshot for snapshot in snapshots
        if parse_utc(snapshot["timestamp"]).timestamp() >= cutoff
    ]


def snapshot_at_window(state, ticker, observed_at, window_seconds=WINDOW_SECONDS):
    """Return a snapshot close enough to exactly one strategy window ago."""
    target = observed_at.timestamp() - window_seconds
    candidates = state["snapshots"].get(ticker, [])
    if not candidates:
        return None
    selected = min(candidates, key=lambda item: abs(parse_utc(item["timestamp"]).timestamp() - target))
    if abs(parse_utc(selected["timestamp"]).timestamp() - target) > MAX_SNAPSHOT_LAG_SECONDS:
        return None
    return {
        "timestamp": parse_utc(selected["timestamp"]),
        "yes_ask": selected["yes_ask"],
    }


def live_signal(game, side, start_quote, end_quote, espn_series):
    """Evaluate one market side using the original delta thresholds."""
    if abs(end_quote["yes_ask"] - start_quote["yes_ask"]) < MIN_KALSHI_MOVE:
        return None
    start_espn = latest_espn_observation(
        espn_series, start_quote["timestamp"], MAX_ESPN_ALIGNMENT_SECONDS
    )
    end_espn = latest_espn_observation(
        espn_series, end_quote["timestamp"], MAX_ESPN_ALIGNMENT_SECONDS
    )
    if not start_espn or not end_espn or start_espn.get("play_id") == end_espn.get("play_id"):
        return None
    signal = calculate_delta_signal(
        start_quote, end_quote, start_espn, end_espn, side, MIN_DELTA_EDGE
    )
    if signal:
        signal["game_id"] = game["espn_id"]
    return signal


def run_cycle(state, now):
    markets = get_nfl_markets()
    games = active_scoreboard_games(now)
    if not games:
        print(f"{now.isoformat()} no NFL games in progress")
        return 0

    fills = 0
    for game in games:
        pair = market_pair(markets, game)
        if not pair:
            print(f"{game['away']} @ {game['home']}: Kalshi market pair not found")
            continue
        quotes = {}
        for side, ticker in pair.items():
            ask = get_yes_bid_ask(ticker)["ask"]
            if ask is None:
                continue
            quotes[side] = {"timestamp": now, "yes_ask": ask}
            record_snapshot(state, ticker, ask, now)
        if set(quotes) != {"home", "away"}:
            print(f"{game['away']} @ {game['home']}: incomplete order book")
            continue
        espn_series = build_espn_probability_series(get_game_data(game["espn_id"]), game["kickoff"])
        for market_side, ticker in pair.items():
            start_quote = snapshot_at_window(state, ticker, now)
            if not start_quote:
                continue
            signal = live_signal(game, market_side, start_quote, quotes[market_side], espn_series)
            if not signal:
                continue
            traded_side = signal["traded_side"]
            end_play = signal.get("espn_end_play_id") or signal["espn_end_timestamp"].isoformat()
            signal_id = f"{game['espn_id']}:{traded_side}:{end_play}"
            if signal_id in state["used_signals"]:
                continue
            entry_price = quotes[traded_side]["yes_ask"]
            stake = edge_to_bet_size(signal["edge"])
            if not 0 < entry_price < 1 or stake <= 0:
                continue
            contracts = stake / entry_price
            trade = {
                "mode": "paper",
                "signal_id": signal_id,
                "recorded_at": now.isoformat(),
                "game": f"{game['away']} @ {game['home']}",
                "market_ticker": pair[traded_side],
                "side": "YES",
                "team_side": traded_side,
                "entry_price": entry_price,
                "stake": stake,
                "contracts": contracts,
                "edge": signal["edge"],
                "kalshi_move": signal["kalshi_delta"],
            }
            state["trades"].append(trade)
            state["used_signals"].append(signal_id)
            fills += 1
            print(f"PAPER BUY {pair[traded_side]} ${stake:.2f} at {entry_price:.3f} (edge {signal['edge']:.1%})")
    return fills


def main():
    parser = argparse.ArgumentParser(description="Paper-trade the NFL delta strategy; never submits an order.")
    parser.add_argument("--once", action="store_true", help="Run a single safe polling cycle.")
    parser.add_argument("--poll-seconds", type=int, default=15, help="Polling interval (default: 15).")
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    args = parser.parse_args()
    if args.poll_seconds <= 0:
        parser.error("--poll-seconds must be positive")
    state = load_state(args.state_file)
    while True:
        try:
            run_cycle(state, utc_now())
            save_state(args.state_file, state)
        except Exception as error:
            # Keep a running paper process alive through a transient provider outage.
            print(f"cycle failed: {type(error).__name__}: {error}")
        if args.once:
            break
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
