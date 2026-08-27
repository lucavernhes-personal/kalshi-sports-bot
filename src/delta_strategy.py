"""
Delta-based Kalshi / ESPN trading strategy.

Strategy
--------
1. Find a Kalshi YES price movement of >=10 percentage points
   over exactly five minutes.
2. Align ESPN win probabilities to the beginning and end of
   that five-minute interval.
3. Require ESPN to have a new play/probability observation
   during the interval.
4. Compare ESPN's probability movement with Kalshi's movement.
5. If the discrepancy exceeds 5 percentage points, trade the
   side ESPN indicates is underpriced.
6. Only one trade is allowed per ESPN end play.
"""

from datetime import datetime, timedelta, timezone


WINDOW_SECONDS = 5 * 60

MIN_KALSHI_MOVE = 0.10

MIN_DELTA_EDGE = 0.05


# ============================================================
# TIMESTAMP UTILITIES
# ============================================================

def parse_utc_timestamp(value):
    """Convert an ISO timestamp or datetime into UTC."""

    if value is None:
        return None

    if isinstance(value, datetime):
        dt = value
    else:
        value = str(value).strip()

        if not value:
            return None

        if value.endswith("Z"):
            value = value[:-1] + "+00:00"

        dt = datetime.fromisoformat(value)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(timezone.utc)


def parse_game_clock(clock_value):
    """Convert an ESPN game clock such as '12:34' into seconds."""

    if clock_value is None:
        return None

    try:
        minutes, seconds = str(clock_value).split(":")
        return int(minutes) * 60 + int(seconds)
    except (ValueError, TypeError):
        return None


def reconstruct_espn_timestamp(game_kickoff, probability_entry):
    """
    Get the timestamp of an ESPN win-probability observation.

    Prefer ESPN wallclock.

    If wallclock is unavailable, reconstruct the timestamp from
    period/game clock.

    NOTE:
    The reconstructed timestamp is only an approximation because
    ESPN game-clock time does not account for stoppages between
    observations.
    """

    kickoff = parse_utc_timestamp(game_kickoff)

    if kickoff is None:
        return None

    play = probability_entry.get("play") or {}

    # --------------------------------------------------------
    # Preferred: ESPN wallclock
    # --------------------------------------------------------

    wallclock = play.get("wallclock")

    if wallclock:
        try:
            return parse_utc_timestamp(wallclock)
        except (ValueError, TypeError):
            pass

    # --------------------------------------------------------
    # Fallback: period + game clock
    # --------------------------------------------------------

    period = play.get("period") or {}

    try:
        period_number = int(
            period.get("number", 1)
        )
    except (TypeError, ValueError):
        period_number = 1

    clock = play.get("clock") or {}

    display_value = clock.get("displayValue")

    remaining = parse_game_clock(display_value)

    if remaining is None:
        return None

    elapsed_before_period = (
        (period_number - 1) * 15 * 60
    )

    elapsed_in_period = (
        15 * 60 - remaining
    )

    return kickoff + timedelta(
        seconds=(
            elapsed_before_period
            + elapsed_in_period
        )
    )


# ============================================================
# ESPN DATA
# ============================================================

def build_espn_probability_series(
    game_data,
    game_kickoff,
):
    """
    Build timestamped ESPN win-probability observations.

    Returns observations sorted chronologically.
    """

    winprobability = game_data.get(
        "winprobability",
        [],
    )

    series = []

    for entry in winprobability:

        if not isinstance(entry, dict):
            continue

        timestamp = reconstruct_espn_timestamp(
            game_kickoff,
            entry,
        )

        if timestamp is None:
            continue

        home_probability = entry.get(
            "homeWinPercentage"
        )

        if home_probability is None:
            continue

        try:
            home_probability = float(
                home_probability
            )
        except (TypeError, ValueError):
            continue

        # ESPN may return either 0-1 or 0-100.
        if home_probability > 1.0:
            home_probability /= 100.0

        home_probability = max(
            0.0,
            min(1.0, home_probability),
        )

        play = entry.get("play") or {}

        period = play.get("period") or {}
        clock = play.get("clock") or {}

        play_id = entry.get("playId")

        series.append({
            "timestamp": timestamp,

            "home_probability":
                home_probability,

            "away_probability":
                1.0 - home_probability,

            "play_id":
                play_id,

            "period":
                period.get("number"),

            "clock":
                clock.get("displayValue"),
        })

    series.sort(
        key=lambda x: x["timestamp"]
    )

    return series


def latest_espn_observation(
    espn_series,
    target_timestamp,
):
    """
    Return the latest ESPN observation at or before
    target_timestamp.
    """

    target = parse_utc_timestamp(
        target_timestamp
    )

    if target is None:
        return None

    latest = None

    for observation in espn_series:

        if observation["timestamp"] <= target:
            latest = observation
        else:
            break

    return latest


# ============================================================
# KALSHI DATA
# ============================================================

def extract_yes_ask(candle):
    """Extract YES ask from a Kalshi candle."""

    yes_ask = candle.get("yes_ask")

    if yes_ask is None:
        return None

    if isinstance(yes_ask, dict):

        value = yes_ask.get(
            "close_dollars"
        )

        if value is None:
            value = yes_ask.get(
                "close"
            )

    else:

        value = yes_ask

    if value is None:
        return None

    try:
        value = float(value)
    except (TypeError, ValueError):
        return None

    # Normalize cents -> dollars.
    if value > 1.0:
        value /= 100.0

    if not 0.0 <= value <= 1.0:
        return None

    return value


def candle_timestamp(candle):
    """Return Kalshi candle end timestamp in Unix seconds."""

    timestamp = candle.get(
        "end_period_ts"
    )

    if timestamp is None:
        return None

    try:
        return int(timestamp)
    except (TypeError, ValueError):
        return None


def normalize_kalshi_candles(candles):
    """
    Normalize raw Kalshi candles.

    Duplicate timestamps are collapsed, with the last candle
    for a timestamp taking precedence.
    """

    observations = {}

    for candle in candles:

        if not isinstance(candle, dict):
            continue

        timestamp = candle_timestamp(
            candle
        )

        ask = extract_yes_ask(
            candle
        )

        if timestamp is None or ask is None:
            continue

        observations[timestamp] = {
            "timestamp":
                datetime.fromtimestamp(
                    timestamp,
                    tz=timezone.utc,
                ),

            "timestamp_ts":
                timestamp,

            "yes_ask":
                ask,

            "candle":
                candle,
        }

    return [
        observations[timestamp]
        for timestamp in sorted(
            observations
        )
    ]


# ============================================================
# KALSHI MOVEMENT DETECTION
# ============================================================

def find_kalshi_moves(
    kalshi_candles,
    window_seconds=WINDOW_SECONDS,
    min_move=MIN_KALSHI_MOVE,
):
    """
    Find non-overlapping Kalshi movements over exactly
    `window_seconds`.

    A movement qualifies when:

        abs(end_price - start_price) >= min_move

    Only candles whose timestamps are exactly
    `window_seconds` apart are considered.
    """

    candles = normalize_kalshi_candles(
        kalshi_candles
    )

    if len(candles) < 2:
        return []

    moves = []

    timestamp_to_observation = {
        candle["timestamp_ts"]: candle
        for candle in candles
    }

    i = 0

    while i < len(candles):

        start = candles[i]

        target_ts = (
            start["timestamp_ts"]
            + int(window_seconds)
        )

        end = timestamp_to_observation.get(
            target_ts
        )

        if end is None:
            i += 1
            continue

        delta = (
            end["yes_ask"]
            - start["yes_ask"]
        )

        if abs(delta) >= min_move:

            moves.append({
                "start": start,
                "end": end,

                "kalshi_delta":
                    delta,

                "absolute_kalshi_move":
                    abs(delta),
            })

            # ------------------------------------------------
            # Make movements non-overlapping.
            # ------------------------------------------------

            i += 1

            while (
                i < len(candles)
                and candles[i]["timestamp_ts"]
                <= end["timestamp_ts"]
            ):
                i += 1

        else:
            i += 1

    return moves


# ============================================================
# DELTA SIGNAL
# ============================================================

def calculate_delta_signal(
    start_kalshi,
    end_kalshi,
    start_espn,
    end_espn,
    kalshi_yes_side,
    min_delta_edge=MIN_DELTA_EDGE,
):
    """
    Compare ESPN and Kalshi probability changes.

    `kalshi_yes_side` identifies which team the Kalshi YES
    contract represents.

    The discrepancy is:

        ESPN movement - Kalshi movement

    A positive discrepancy means ESPN moved more toward the
    Kalshi YES side than Kalshi did.

    A negative discrepancy means Kalshi moved more toward the
    YES side than ESPN did, so the opposite side is considered
    underpriced.
    """

    if kalshi_yes_side not in {
        "home",
        "away",
    }:
        raise ValueError(
            "kalshi_yes_side must be "
            "'home' or 'away'"
        )

    if start_kalshi is None or end_kalshi is None:
        return None

    if start_espn is None or end_espn is None:
        return None

    # --------------------------------------------------------
    # Kalshi movement
    # --------------------------------------------------------

    kalshi_delta = (
        end_kalshi["yes_ask"]
        - start_kalshi["yes_ask"]
    )

    # --------------------------------------------------------
    # ESPN movement for the same team represented by YES.
    # --------------------------------------------------------

    if kalshi_yes_side == "home":

        espn_yes_delta = (
            end_espn["home_probability"]
            - start_espn["home_probability"]
        )

    else:

        espn_yes_delta = (
            end_espn["away_probability"]
            - start_espn["away_probability"]
        )

    # --------------------------------------------------------
    # Compare movements.
    # --------------------------------------------------------

    discrepancy = (
        espn_yes_delta
        - kalshi_delta
    )

    if abs(discrepancy) <= min_delta_edge:
        return None

    # --------------------------------------------------------
    # Determine which side is underpriced.
    #
    # Positive discrepancy:
    #     ESPN moved further toward YES.
    #     YES is underpriced.
    #
    # Negative discrepancy:
    #     Kalshi moved further toward YES.
    #     NO is underpriced.
    # --------------------------------------------------------

    if discrepancy > 0:

        traded_side = kalshi_yes_side

    else:

        traded_side = (
            "away"
            if kalshi_yes_side == "home"
            else "home"
        )

    return {
        "kalshi_delta":
            kalshi_delta,

        "espn_yes_delta":
            espn_yes_delta,

        "delta_discrepancy":
            discrepancy,

        "edge":
            abs(discrepancy),

        "kalshi_yes_side":
            kalshi_yes_side,

        "traded_side":
            traded_side,

        "interval_start":
            start_kalshi["timestamp"],

        "interval_end":
            end_kalshi["timestamp"],

        "kalshi_start":
            start_kalshi["yes_ask"],

        "kalshi_end":
            end_kalshi["yes_ask"],

        "espn_home_start":
            start_espn["home_probability"],

        "espn_home_end":
            end_espn["home_probability"],

        "espn_away_start":
            start_espn["away_probability"],

        "espn_away_end":
            end_espn["away_probability"],

        "espn_start_timestamp":
            start_espn["timestamp"],

        "espn_end_timestamp":
            end_espn["timestamp"],

        "espn_start_play_id":
            start_espn.get("play_id"),

        "espn_end_play_id":
            end_espn.get("play_id"),

        "espn_start_period":
            start_espn.get("period"),

        "espn_end_period":
            end_espn.get("period"),

        "espn_start_clock":
            start_espn.get("clock"),

        "espn_end_clock":
            end_espn.get("clock"),
    }


# ============================================================
# SIGNAL GENERATION
# ============================================================

def generate_delta_signals(
    game_kickoff,
    game_data,
    kalshi_candles,
    kalshi_yes_side,
    window_seconds=WINDOW_SECONDS,
    min_kalshi_move=MIN_KALSHI_MOVE,
    min_delta_edge=MIN_DELTA_EDGE,
):
    """
    Generate delta signals.

    Rules:

    1. Kalshi must move >= min_kalshi_move over exactly
       window_seconds.
    2. ESPN observations must exist at both endpoints.
    3. ESPN must have changed to a different play between
       the endpoints.
    4. Only one signal may be generated for a given ESPN
       end play.
    """

    espn_series = build_espn_probability_series(
        game_data,
        game_kickoff,
    )

    if not espn_series:
        return []

    moves = find_kalshi_moves(
        kalshi_candles,
        window_seconds=window_seconds,
        min_move=min_kalshi_move,
    )

    if not moves:
        return []

    signals = []

    # Prevent multiple trades caused by the same ESPN play.
    used_espn_plays = set()

    for move in moves:

        start_kalshi = move["start"]
        end_kalshi = move["end"]

        # ----------------------------------------------------
        # Align ESPN observations to the Kalshi interval.
        # ----------------------------------------------------

        start_espn = latest_espn_observation(
            espn_series,
            start_kalshi["timestamp"],
        )

        end_espn = latest_espn_observation(
            espn_series,
            end_kalshi["timestamp"],
        )

        if (
            start_espn is None
            or end_espn is None
        ):
            continue

        # ----------------------------------------------------
        # ESPN must have produced a new play.
        #
        # If the same play is the latest observation at both
        # endpoints, we don't actually have a new ESPN event
        # explaining the Kalshi movement.
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
        # If ESPN has no play IDs, fall back to timestamps.
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
        # Only one trade per ESPN end play.
        # ----------------------------------------------------

        if end_play_id is not None:

            if end_play_id in used_espn_plays:
                continue

            used_espn_plays.add(
                end_play_id
            )

        # ----------------------------------------------------
        # Calculate discrepancy.
        # ----------------------------------------------------

        signal = calculate_delta_signal(
            start_kalshi=start_kalshi,
            end_kalshi=end_kalshi,
            start_espn=start_espn,
            end_espn=end_espn,
            kalshi_yes_side=kalshi_yes_side,
            min_delta_edge=min_delta_edge,
        )

        if signal is None:
            continue

        signal["kalshi_move"] = (
            move["absolute_kalshi_move"]
        )

        signals.append(signal)

    return signals


# ============================================================
# ENTRY PRICE
# ============================================================

def select_entry_price(signal):
    """
    Return the YES/NO price of the side actually traded.

    If trading the Kalshi YES side:

        entry = Kalshi YES ask

    If trading the opposite side:

        entry = 1 - Kalshi YES ask
    """

    if (
        signal["traded_side"]
        == signal["kalshi_yes_side"]
    ):

        return signal["kalshi_end"]

    return 1.0 - signal["kalshi_end"]


# ============================================================
# BET SIZING
# ============================================================

def edge_to_bet_size(edge):
    """
    Convert delta edge into bet size.

        <5%       -> $0
        5-6%      -> $10
        6-8%      -> $15
        >=8%      -> $20
    """

    edge = float(edge)

    if edge < 0.05:
        return 0.0

    if edge < 0.06:
        return 10.0

    if edge < 0.08:
        return 15.0

    return 20.0