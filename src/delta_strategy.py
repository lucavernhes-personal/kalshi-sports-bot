"""
Delta-based Kalshi / ESPN trading strategy.

IMPORTANT TIMESTAMP RULE:

ESPN observations are matched to Kalshi timestamps using
the ACTUAL ESPN play wallclock only.

Game-clock reconstruction is intentionally NOT used.

A signal is valid only if the ESPN observation used at
EACH endpoint occurred no more than MAX_ESPN_ALIGNMENT_SECONDS
before the corresponding Kalshi timestamp.
"""

from datetime import datetime, timezone
from src.espn import build_espn_probability_series


# ============================================================
# STRATEGY CONSTANTS
# ============================================================

WINDOW_SECONDS = 5 * 60

MIN_KALSHI_MOVE = 0.10

MIN_DELTA_EDGE = 0.05

# Maximum allowed difference between the Kalshi endpoint
# and the ESPN wallclock observation used for that endpoint.
MAX_ESPN_ALIGNMENT_SECONDS = 30


# ============================================================
# TIMESTAMP UTILITIES
# ============================================================

def parse_utc_timestamp(value):
    """
    Convert an ISO timestamp or datetime into UTC.

    No game-clock reconstruction is performed anywhere
    in this module.
    """

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
        dt = dt.replace(
            tzinfo=timezone.utc
        )

    return dt.astimezone(
        timezone.utc
    )


# ============================================================
# ESPN DATA
# ============================================================

def build_espn_probability_series(
    game_data,
    game_kickoff=None,
):
    """
    Build timestamped ESPN win-probability observations.

    CRITICAL:

    The ONLY timestamp accepted here is the actual
    wallclock timestamp attached to the ESPN play.

    We intentionally do NOT reconstruct timestamps from:

        - game kickoff
        - quarter
        - game clock
        - period
        - elapsed game time

    If an ESPN probability observation does not have a
    valid play wallclock, it is discarded.
    """

    winprobability = game_data.get(
        "winprobability",
        [],
    )

    series = []

    for entry in winprobability:

        if not isinstance(entry, dict):
            continue

        # ----------------------------------------------------
        # Find the associated ESPN play
        # ----------------------------------------------------

        play = entry.get("play")

        if not isinstance(play, dict):
            play = {}

        # ----------------------------------------------------
        # ACTUAL ESPN WALLCLOCK ONLY
        # ----------------------------------------------------

        wallclock = play.get(
            "wallclock"
        )

        if not wallclock:
            continue

        try:

            timestamp = parse_utc_timestamp(
                wallclock
            )

        except (
            ValueError,
            TypeError,
        ):

            continue

        if timestamp is None:
            continue

        # ----------------------------------------------------
        # Home probability
        # ----------------------------------------------------

        home_probability = entry.get(
            "homeWinPercentage"
        )

        if home_probability is None:
            continue

        try:

            home_probability = float(
                home_probability
            )

        except (
            TypeError,
            ValueError,
        ):

            continue

        # ESPN may return either:
        #
        #   0.54
        #
        # or:
        #
        #   54.0

        if home_probability > 1.0:
            home_probability /= 100.0

        home_probability = max(
            0.0,
            min(
                1.0,
                home_probability,
            ),
        )

        # ----------------------------------------------------
        # Play metadata
        # ----------------------------------------------------

        period = play.get(
            "period"
        ) or {}

        clock = play.get(
            "clock"
        ) or {}

        play_id = entry.get(
            "playId"
        )

        if play_id is None:
            play_id = play.get(
                "id"
            )

        series.append({

            # ------------------------------------------------
            # PRIMARY TIMESTAMP
            #
            # This is ALWAYS the actual ESPN wallclock.
            # ------------------------------------------------

            "timestamp": timestamp,

            "wallclock": timestamp,

            # ------------------------------------------------
            # Probabilities
            # ------------------------------------------------

            "home_probability":
                home_probability,

            "away_probability":
                1.0 - home_probability,

            # ------------------------------------------------
            # ESPN metadata
            # ------------------------------------------------

            "play_id":
                play_id,

            "period":
                period.get("number"),

            "clock":
                clock.get("displayValue"),

            "play_text":
                play.get("text"),

        })

    # --------------------------------------------------------
    # Sort chronologically by ACTUAL WALLCLOCK
    # --------------------------------------------------------

    series.sort(
        key=lambda x: x["timestamp"]
    )

    return series


# ============================================================
# ESPN / KALSHI TIMESTAMP ALIGNMENT
# ============================================================

def latest_espn_observation(
    espn_series,
    target_timestamp,
    max_alignment_seconds=MAX_ESPN_ALIGNMENT_SECONDS,
):
    """
    Return the latest ESPN wallclock observation at or before
    target_timestamp, provided it is no more than
    max_alignment_seconds old.

    IMPORTANT:

    This function NEVER falls back to game-clock reconstruction.

    If the nearest valid prior ESPN wallclock is too old,
    return None.

    Example:

        Kalshi endpoint: 01:54:00
        ESPN observation: 01:53:47

        Difference = 13 sec
        -> VALID

        Kalshi endpoint: 01:54:00
        ESPN observation: 01:41:13

        Difference = 767 sec
        -> INVALID
    """

    target = parse_utc_timestamp(
        target_timestamp
    )

    if target is None:
        return None

    latest = None

    for observation in espn_series:

        timestamp = observation.get(
            "timestamp"
        )

        if timestamp is None:
            continue

        if timestamp <= target:

            latest = observation

        else:

            # Series is sorted chronologically.
            break

    if latest is None:
        return None

    # --------------------------------------------------------
    # Calculate actual wallclock difference
    # --------------------------------------------------------

    age_seconds = (
        target
        - latest["timestamp"]
    ).total_seconds()

    # ESPN observation must not be after the Kalshi
    # endpoint and must not be more than 30 seconds old.
    if age_seconds < 0:
        return None

    if age_seconds > max_alignment_seconds:
        return None

    return latest


# ============================================================
# KALSHI DATA
# ============================================================

def extract_yes_ask(candle):
    """
    Extract YES ask from a Kalshi candle.
    """

    yes_ask = candle.get(
        "yes_ask"
    )

    if yes_ask is None:
        return None

    if isinstance(
        yes_ask,
        dict,
    ):

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

        value = float(
            value
        )

    except (
        TypeError,
        ValueError,
    ):

        return None

    # Kalshi can return cents.
    if value > 1.0:
        value /= 100.0

    if not 0.0 <= value <= 1.0:
        return None

    return value


def candle_timestamp(candle):
    """
    Return Kalshi candle end timestamp in Unix seconds.
    """

    timestamp = candle.get(
        "end_period_ts"
    )

    if timestamp is None:
        return None

    try:

        return int(
            timestamp
        )

    except (
        TypeError,
        ValueError,
    ):

        return None


def normalize_kalshi_candles(candles):
    """
    Normalize raw Kalshi candles.

    Duplicate timestamps are collapsed, with the last
    observation taking precedence.
    """

    observations = {}

    for candle in candles:

        if not isinstance(
            candle,
            dict,
        ):
            continue

        timestamp = candle_timestamp(
            candle
        )

        ask = extract_yes_ask(
            candle
        )

        if (
            timestamp is None
            or ask is None
        ):
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
    window_seconds.

    A movement qualifies when:

        abs(end_price - start_price) >= min_move
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

                "start":
                    start,

                "end":
                    end,

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

    Positive discrepancy:

        ESPN moved more toward YES than Kalshi.

        -> YES is underpriced.

    Negative discrepancy:

        Kalshi moved more toward YES than ESPN.

        -> NO is underpriced.
    """

    if kalshi_yes_side not in {
        "home",
        "away",
    }:

        raise ValueError(
            "kalshi_yes_side must be "
            "'home' or 'away'"
        )

    if (
        start_kalshi is None
        or end_kalshi is None
    ):
        return None

    if (
        start_espn is None
        or end_espn is None
    ):
        return None

    # --------------------------------------------------------
    # Kalshi movement
    # --------------------------------------------------------

    kalshi_delta = (
        end_kalshi["yes_ask"]
        - start_kalshi["yes_ask"]
    )

    # --------------------------------------------------------
    # ESPN movement for the team represented by YES
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
    # Difference between ESPN and Kalshi movement
    # --------------------------------------------------------

    discrepancy = (
        espn_yes_delta
        - kalshi_delta
    )

    # Require strictly greater than 5%.
    if abs(discrepancy) <= min_delta_edge:
        return None

    # --------------------------------------------------------
    # Determine underpriced side
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

        # ----------------------------------------------------
        # Core delta values
        # ----------------------------------------------------

        "kalshi_delta":
            kalshi_delta,

        "espn_yes_delta":
            espn_yes_delta,

        "delta_discrepancy":
            discrepancy,

        "edge":
            abs(discrepancy),

        # ----------------------------------------------------
        # Sides
        # ----------------------------------------------------

        "kalshi_yes_side":
            kalshi_yes_side,

        "traded_side":
            traded_side,

        # ----------------------------------------------------
        # Kalshi timestamps/prices
        # ----------------------------------------------------

        "interval_start":
            start_kalshi["timestamp"],

        "interval_end":
            end_kalshi["timestamp"],

        "kalshi_start":
            start_kalshi["yes_ask"],

        "kalshi_end":
            end_kalshi["yes_ask"],

        # ----------------------------------------------------
        # ESPN probabilities
        # ----------------------------------------------------

        "espn_home_start":
            start_espn["home_probability"],

        "espn_home_end":
            end_espn["home_probability"],

        "espn_away_start":
            start_espn["away_probability"],

        "espn_away_end":
            end_espn["away_probability"],

        # ----------------------------------------------------
        # ACTUAL ESPN WALLCLOCK TIMESTAMPS
        # ----------------------------------------------------

        "espn_start_timestamp":
            start_espn["timestamp"],

        "espn_end_timestamp":
            end_espn["timestamp"],

        # ----------------------------------------------------
        # ESPN metadata
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # Explicit alignment diagnostics
        # ----------------------------------------------------

        "espn_start_alignment_seconds":
            (
                start_kalshi["timestamp"]
                - start_espn["timestamp"]
            ).total_seconds(),

        "espn_end_alignment_seconds":
            (
                end_kalshi["timestamp"]
                - end_espn["timestamp"]
            ).total_seconds(),
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
    max_espn_alignment_seconds=MAX_ESPN_ALIGNMENT_SECONDS,
    verbose=False,
):
    """
    Generate delta signals.

    ESPN probability data is parsed by src.espn.
    Only observations with genuine ESPN wallclock timestamps
    are eligible for endpoint alignment.

    If verbose=True, print why qualifying Kalshi movements
    are rejected.
    """

    # ========================================================
    # BUILD ESPN SERIES
    # ========================================================

    espn_series = build_espn_probability_series(
        game_data,
        game_kickoff,
    )

    if not espn_series:

        if verbose:
            print("  REJECT: no ESPN probability observations")

        return []

    # ========================================================
    # KEEP ONLY ACTUAL ESPN WALLCLOCK OBSERVATIONS
    # ========================================================

    wallclock_series = []

    for observation in espn_series:

        timestamp_source = observation.get(
            "timestamp_source"
        )

        wallclock = observation.get(
            "wallclock"
        )

        timestamp = observation.get(
            "timestamp"
        )

        if timestamp_source != "ESPN_WALLCLOCK":
            continue

        if wallclock is None:
            continue

        if timestamp is None:
            continue

        observation = dict(observation)

        observation["timestamp"] = wallclock

        wallclock_series.append(observation)

    wallclock_series.sort(
        key=lambda x: x["timestamp"]
    )

    if not wallclock_series:

        if verbose:
            print(
                "  REJECT: ESPN series contains no "
                "actual wallclock observations"
            )

        return []

    if verbose:
        print(
            f"  ESPN wallclock observations: "
            f"{len(wallclock_series)}"
        )

    # ========================================================
    # FIND KALSHI MOVEMENTS
    # ========================================================

    moves = find_kalshi_moves(
        kalshi_candles,
        window_seconds=window_seconds,
        min_move=min_kalshi_move,
    )

    if verbose:
        print(
            f"  Qualifying Kalshi >= "
            f"{min_kalshi_move:.0%} moves: {len(moves)}"
        )

    if not moves:
        return []

    signals = []

    used_espn_plays = set()

    # ========================================================
    # ALIGN KALSHI ENDPOINTS TO ESPN
    # ========================================================

    for number, move in enumerate(moves, 1):

        start_kalshi = move["start"]
        end_kalshi = move["end"]

        if verbose:
            print()
            print(
                f"  CANDIDATE #{number}"
            )
            print(
                f"    Kalshi: "
                f"{start_kalshi['timestamp'].isoformat()} -> "
                f"{end_kalshi['timestamp'].isoformat()}"
            )
            print(
                f"    Kalshi move: "
                f"{move['kalshi_delta']:+.4f}"
            )

        # ====================================================
        # START ESPN
        # ====================================================

        start_espn = latest_espn_observation(
            wallclock_series,
            start_kalshi["timestamp"],
            max_alignment_seconds=max_espn_alignment_seconds,
        )

        if start_espn is None:

            if verbose:
                print(
                    f"    REJECT: no ESPN observation within "
                    f"{max_espn_alignment_seconds}s of start"
                )

            continue

        start_age = (
            start_kalshi["timestamp"]
            - start_espn["timestamp"]
        ).total_seconds()

        # ====================================================
        # END ESPN
        # ====================================================

        end_espn = latest_espn_observation(
            wallclock_series,
            end_kalshi["timestamp"],
            max_alignment_seconds=max_espn_alignment_seconds,
        )

        if end_espn is None:

            if verbose:
                print(
                    f"    START ESPN: "
                    f"{start_espn['timestamp'].isoformat()} "
                    f"({start_age:.0f}s before Kalshi)"
                )
                print(
                    f"    REJECT: no ESPN observation within "
                    f"{max_espn_alignment_seconds}s of end"
                )

            continue

        end_age = (
            end_kalshi["timestamp"]
            - end_espn["timestamp"]
        ).total_seconds()

        if verbose:
            print(
                f"    ESPN start: "
                f"{start_espn['timestamp'].isoformat()} "
                f"({start_age:.0f}s before Kalshi)"
            )
            print(
                f"    ESPN end:   "
                f"{end_espn['timestamp'].isoformat()} "
                f"({end_age:.0f}s before Kalshi)"
            )

        # ====================================================
        # REQUIRE DIFFERENT ESPN PLAYS
        # ====================================================

        start_play_id = start_espn.get("play_id")
        end_play_id = end_espn.get("play_id")

        if (
            start_play_id is not None
            and end_play_id is not None
            and start_play_id == end_play_id
        ):

            if verbose:
                print(
                    f"    REJECT: same ESPN play at both endpoints "
                    f"({start_play_id})"
                )

            continue

        # ====================================================
        # REQUIRE ESPN TIME TO ADVANCE
        # ====================================================

        if (
            start_play_id is None
            or end_play_id is None
        ):

            if (
                start_espn["timestamp"]
                >= end_espn["timestamp"]
            ):

                if verbose:
                    print(
                        "    REJECT: ESPN timestamps did not advance"
                    )

                continue

        # ====================================================
        # ONE SIGNAL PER END PLAY
        # ====================================================

        if end_play_id is not None:

            if end_play_id in used_espn_plays:

                if verbose:
                    print(
                        f"    REJECT: duplicate ESPN end play "
                        f"{end_play_id}"
                    )

                continue

            used_espn_plays.add(end_play_id)

        # ====================================================
        # CALCULATE SIGNAL
        # ====================================================

        signal = calculate_delta_signal(
            start_kalshi=start_kalshi,
            end_kalshi=end_kalshi,
            start_espn=start_espn,
            end_espn=end_espn,
            kalshi_yes_side=kalshi_yes_side,
            min_delta_edge=min_delta_edge,
        )

        if signal is None:

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

            if verbose:
                print(
                    f"    ESPN YES delta: "
                    f"{espn_delta:+.4f}"
                )
                print(
                    f"    Kalshi delta:   "
                    f"{kalshi_delta:+.4f}"
                )
                print(
                    f"    Discrepancy:    "
                    f"{discrepancy:+.4f}"
                )
                print(
                    f"    Edge:            "
                    f"{abs(discrepancy):.4f}"
                )
                print(
                    f"    REJECT: edge <= "
                    f"{min_delta_edge:.2%}"
                )

            continue

        # ====================================================
        # VALID SIGNAL
        # ====================================================

        signal["kalshi_move"] = (
            move["absolute_kalshi_move"]
        )

        signal["espn_timestamp_source"] = (
            "ESPN_WALLCLOCK"
        )

        signals.append(signal)

        if verbose:
            print(
                f"    >>> VALID SIGNAL "
                f"(edge={signal['edge']:.4f}, "
                f"trade={signal['traded_side'].upper()})"
            )

    return signals


# ============================================================
# ENTRY PRICE
# ============================================================

def select_entry_price(signal):
    """
    Return the price of the side actually traded.

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

    return (
        1.0
        - signal["kalshi_end"]
    )


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