import requests
from dataclasses import dataclass
from typing import Optional


BASE_URL = "https://external-api.kalshi.com/trade-api/v2"


@dataclass
class KalshiMarket:
    ticker: str
    title: str
    yes_bid: Optional[int]
    yes_ask: Optional[int]
    no_bid: Optional[int]
    no_ask: Optional[int]


def get_nfl_game_markets() -> list[KalshiMarket]:
    """Fetch NFL game winner markets from Kalshi."""

    url = f"{BASE_URL}/markets"

    params = {
        "series_ticker": "KXNFLGAME",
        "limit": 100,
    }

    response = requests.get(url, params=params, timeout=10)
    response.raise_for_status()

    markets = response.json().get("markets", [])

    return [
        KalshiMarket(
            ticker=market["ticker"],
            title=market["title"],
            yes_bid=market.get("yes_bid"),
            yes_ask=market.get("yes_ask"),
            no_bid=market.get("no_bid"),
            no_ask=market.get("no_ask"),
        )
        for market in markets
    ]


if __name__ == "__main__":
    markets = get_nfl_game_markets()

    print(f"Found {len(markets)} NFL game markets\n")

    for market in markets[:20]:
        print(
            f"{market.ticker} | "
            f"{market.title} | "
            f"YES bid: {market.yes_bid} | "
            f"YES ask: {market.yes_ask}"
        )
