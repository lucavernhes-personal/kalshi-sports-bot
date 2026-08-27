import requests


KALSHI_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"


def get_historical_nfl_markets():
    url = f"{KALSHI_BASE_URL}/historical/markets"

    params = {
        "series_ticker": "KXNFLGAME",
        "limit": 1000,
    }

    response = requests.get(
        url,
        params=params,
        timeout=30,
    )

    if not response.ok:
        print("Kalshi API error:")
        print(response.text)
        response.raise_for_status()

    return response.json()


def main():
    data = get_historical_nfl_markets()

    markets = data.get("markets", [])

    print(f"Found {len(markets)} historical NFL markets")
    print()

    for market in markets:
        print(
            market.get("ticker"),
            "|",
            market.get("title"),
            "|",
            "result:",
            market.get("result"),
        )


if __name__ == "__main__":
    main()