import random
import time

import requests


BASE_URL = "https://external-api.kalshi.com/trade-api/v2"

REQUEST_TIMEOUT = 15

# Kalshi's historical candlestick endpoint can be unreliable with
# very large 1-minute windows, so retrieve data in manageable chunks.
CANDLESTICK_CHUNK_SECONDS = 60 * 60  # 1 hour

# ------------------------------------------------------------
# RATE LIMITING
# ------------------------------------------------------------

# Minimum delay between requests.
REQUEST_DELAY = 2.0

# Small random jitter prevents requests from occurring at
# perfectly regular intervals.
REQUEST_JITTER = 0.5

# Retry delays after HTTP 429.
RATE_LIMIT_RETRY_DELAYS = [
    5,
    10,
    20,
    40,
]


# Track the time of the most recent request globally so that
# multiple calls to get_historical_candlesticks() are throttled.
_last_request_time = 0.0


def _wait_before_request():
    """
    Enforce a minimum delay between Kalshi API requests.
    """

    global _last_request_time

    now = time.monotonic()

    elapsed = now - _last_request_time

    if elapsed < REQUEST_DELAY:
        time.sleep(
            REQUEST_DELAY - elapsed
        )

    # Add a small random delay.
    time.sleep(
        random.uniform(
            0.0,
            REQUEST_JITTER,
        )
    )

    _last_request_time = time.monotonic()


def _make_request(url, params):
    """
    Make a Kalshi API request with rate limiting and
    automatic retry handling for HTTP 429.
    """

    for attempt in range(
        len(RATE_LIMIT_RETRY_DELAYS) + 1
    ):

        _wait_before_request()

        try:

            response = requests.get(
                url,
                params=params,
                timeout=REQUEST_TIMEOUT,
            )

        except requests.RequestException as e:

            # Retry transient network errors using the same
            # backoff schedule as rate-limit errors.
            if attempt < len(RATE_LIMIT_RETRY_DELAYS):

                delay = RATE_LIMIT_RETRY_DELAYS[attempt]

                print(
                    f"Kalshi request error: {e}. "
                    f"Retrying in {delay}s..."
                )

                time.sleep(delay)

                continue

            raise RuntimeError(
                f"Kalshi historical request failed: {e}"
            )

        # ----------------------------------------------------
        # SUCCESS
        # ----------------------------------------------------

        if response.ok:
            return response

        # ----------------------------------------------------
        # RATE LIMITED
        # ----------------------------------------------------

        if response.status_code == 429:

            if attempt >= len(RATE_LIMIT_RETRY_DELAYS):

                raise RuntimeError(
                    f"Kalshi historical request failed: "
                    f"{response.status_code} "
                    f"{response.text}"
                )

            delay = RATE_LIMIT_RETRY_DELAYS[attempt]

            print(
                f"Kalshi rate limited (429). "
                f"Retrying in {delay}s..."
            )

            time.sleep(delay)

            continue

        # ----------------------------------------------------
        # OTHER HTTP ERROR
        # ----------------------------------------------------

        raise RuntimeError(
            f"Kalshi historical request failed: "
            f"{response.status_code} "
            f"{response.text}"
        )

    raise RuntimeError(
        "Kalshi historical request failed after retries."
    )


def get_candle_at_time(ticker, target_ts):
    """
    Get the most recent 1-minute candlestick ending at or
    before target_ts.
    """

    start_ts = int(target_ts) - 10 * 60
    end_ts = int(target_ts) + 60

    candles = get_historical_candlesticks(
        ticker=ticker,
        start_ts=start_ts,
        end_ts=end_ts,
        period_interval=1,
    )

    valid_candles = [
        candle
        for candle in candles
        if (
            candle.get("end_period_ts") is not None
            and int(candle["end_period_ts"]) <= int(target_ts)
        )
    ]

    if not valid_candles:
        return None

    return max(
        valid_candles,
        key=lambda x: int(x["end_period_ts"]),
    )


def get_historical_candlesticks(
    ticker,
    start_ts,
    end_ts,
    period_interval=1,
):
    """
    Get historical Kalshi candlesticks.

    For 1-minute candles, requests are split into one-hour chunks
    to avoid API limitations on large historical windows.

    Requests are automatically throttled and retried if Kalshi
    returns HTTP 429.

    period_interval:
        1    = 1 minute
        60   = 1 hour
        1440 = 1 day
    """

    url = (
        f"{BASE_URL}/historical/markets/"
        f"{ticker}/candlesticks"
    )

    all_candlesticks = []

    current_ts = int(start_ts)
    end_ts = int(end_ts)

    while current_ts < end_ts:

        chunk_end = min(
            current_ts + CANDLESTICK_CHUNK_SECONDS,
            end_ts,
        )

        params = {
            "start_ts": current_ts,
            "end_ts": chunk_end,
            "period_interval": period_interval,
        }

        response = _make_request(
            url=url,
            params=params,
        )

        data = response.json()

        all_candlesticks.extend(
            data.get("candlesticks", [])
        )

        # Avoid accidentally requesting the same boundary twice.
        current_ts = chunk_end

    # Remove duplicate candles that can occur at chunk boundaries.
    unique = {}

    for candle in all_candlesticks:

        timestamp = candle.get("end_period_ts")

        if timestamp is not None:
            unique[int(timestamp)] = candle

    return [
        unique[timestamp]
        for timestamp in sorted(unique)
    ]


def extract_yes_ask(candle):
    """
    Extract the closing YES ask from a Kalshi candlestick.

    Handles both:
        {"close": ...}
    and:
        {"close_dollars": ...}

    formats.
    """

    yes_ask = candle.get("yes_ask")

    if yes_ask is None:
        return None

    # Newer Kalshi format.
    if isinstance(yes_ask, dict):

        value = yes_ask.get("close_dollars")

        if value is None:
            value = yes_ask.get("close")

    # Older/simple format.
    else:
        value = yes_ask

    if value is None:
        return None

    value = float(value)

    # Older API responses may return cents rather than dollars.
    if value > 1:
        value /= 100.0

    return value


def get_candlesticks(
    ticker,
    start_ts,
    end_ts,
):
    """
    Get 1-minute historical Kalshi candlesticks for a time window.

    Compatibility wrapper used by the delta strategy backtest.
    The underlying request is handled by the chunked
    get_historical_candlesticks() function.
    """

    return get_historical_candlesticks(
        ticker=ticker,
        start_ts=start_ts,
        end_ts=end_ts,
        period_interval=1,
    )


def get_ask_at_time(ticker, target_ts):
    """
    Get the most recent usable YES ask available at or before
    target_ts.

    Uses 1-minute historical candlesticks.
    """

    # Give ourselves a 10-minute lookback window.
    start_ts = int(target_ts) - 10 * 60
    end_ts = int(target_ts) + 60

    candles = get_historical_candlesticks(
        ticker=ticker,
        start_ts=start_ts,
        end_ts=end_ts,
        period_interval=1,
    )

    if not candles:
        return None

    valid_candles = [
        candle
        for candle in candles
        if (
            candle.get("end_period_ts") is not None
            and int(candle["end_period_ts"]) <= int(target_ts)
        )
    ]

    if not valid_candles:
        return None

    candle = max(
        valid_candles,
        key=lambda x: int(x["end_period_ts"]),
    )

    return extract_yes_ask(candle)


if __name__ == "__main__":
    print("Historical Kalshi module loaded successfully.")