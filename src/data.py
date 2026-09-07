import requests
from collections import defaultdict


KALSHI_MARKETS_URL = "https://api.elections.kalshi.com/trade-api/v2/markets"


def get_nfl_markets():
    """
    Get active NFL game markets from Kalshi.
    """

    markets = []
    cursor = None

    # The public API returns the newest markets first.  Opening-week games can
    # be on later pages once the rest of the season is listed, so follow the
    # cursor instead of silently inspecting only the first 100.
    while True:
        params = {
            "series_ticker": "KXNFLGAME",
            "limit": 1000,
        }
        if cursor:
            params["cursor"] = cursor

        response = requests.get(
            KALSHI_MARKETS_URL,
            params=params,
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        markets.extend(data.get("markets", []))
        cursor = data.get("cursor")
        if not cursor:
            return markets


def get_orderbook(ticker):
    """
    Get the order book for a Kalshi market.
    """

    url = (
        f"https://api.elections.kalshi.com"
        f"/trade-api/v2/markets/{ticker}/orderbook"
    )

    response = requests.get(url, timeout=10)
    response.raise_for_status()

    return response.json()["orderbook_fp"]


def get_yes_bid_ask(ticker):
    """
    Return YES bid and ask prices.

    Prices are returned as decimals.

    Example:
        {
            "bid": 0.19,
            "ask": 0.23
        }
    """

    orderbook = get_orderbook(ticker)

    yes_orders = orderbook.get("yes_dollars", [])
    no_orders = orderbook.get("no_dollars", [])

    # YES bid = highest YES price someone is willing to buy
    yes_bid = None

    if yes_orders:
        yes_bid = max(float(price) for price, _ in yes_orders)

    # YES ask = 1 - highest NO bid
    yes_ask = None

    if no_orders:
        highest_no_bid = max(float(price) for price, _ in no_orders)
        yes_ask = 1 - highest_no_bid

    return {
        "bid": yes_bid,
        "ask": yes_ask,
    }


def parse_market_ticker(ticker):
    """
    Parse a Kalshi NFL game ticker.

    Example:

        KXNFLGAME-26SEP21NYGLAR-NYG

    becomes:

        game = 26SEP21NYGLAR
        team = NYG
    """

    parts = ticker.split("-")

    if len(parts) != 3:
        return None

    return {
        "game": parts[1],
        "team": parts[2],
    }


def get_nfl_market_data():
    """
    Get Kalshi NFL markets grouped by game.
    """

    markets = get_nfl_markets()

    games = defaultdict(dict)

    for market in markets:

        ticker = market.get("ticker")

        if not ticker:
            continue

        parsed = parse_market_ticker(ticker)

        if not parsed:
            continue

        game_id = parsed["game"]
        team = parsed["team"]

        try:
            prices = get_yes_bid_ask(ticker)
        except Exception as e:
            print(f"Could not get order book for {ticker}: {e}")
            continue

        games[game_id][team] = {
            "ticker": ticker,
            "yes_bid": prices["bid"],
            "yes_ask": prices["ask"],
        }

    return dict(games)


if __name__ == "__main__":

    games = get_nfl_market_data()

    print(f"Found {len(games)} NFL games\n")

    for game_id, teams in games.items():

        print(f"Game: {game_id}")

        for team, market in teams.items():

            print(
                f"  {team}: "
                f"YES bid={market['yes_bid']}, "
                f"YES ask={market['yes_ask']}"
            )

        print()
