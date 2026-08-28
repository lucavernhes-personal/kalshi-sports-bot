import requests
from datetime import datetime, timedelta, timezone


ESPN_SCOREBOARD_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
)

ESPN_GAME_URL = "https://cdn.espn.com/core/nfl/game"

REGULAR_SEASON_START = datetime(2026, 9, 10, tzinfo=timezone.utc)

HEADERS = {
    "User-Agent": "curl/8.0",
    "Accept": "application/json",
}


KALSHI_TO_ESPN = {
    "WAS": "WSH",
    "JAC": "JAX",
    "LA": "LAR",
}


def normalize_team_for_espn(team):
    team = team.upper()
    return KALSHI_TO_ESPN.get(team, team)


def get_scoreboard(date):
    date_string = date.strftime("%Y%m%d")

    response = requests.get(
        ESPN_SCOREBOARD_URL,
        params={"dates": date_string},
        headers=HEADERS,
        timeout=10,
    )

    response.raise_for_status()

    return response.json()


def get_game_data(game_id):
    response = requests.get(
        ESPN_GAME_URL,
        params={
            "xhr": "1",
            "gameId": game_id,
        },
        headers=HEADERS,
        timeout=10,
    )

    response.raise_for_status()

    data = response.json()

    gamepackage = data.get("gamepackageJSON")

    if not gamepackage:
        raise ValueError(
            f"No gamepackageJSON found for ESPN game {game_id}"
        )

    return gamepackage


# ============================================================
# PLAY LOOKUP
# ============================================================

def build_play_lookup(game_data):
    """
    Build a lookup from ESPN play ID -> full play object.

    This is important because the winprobability entries do not
    always contain the complete play information, including
    wallclock.
    """

    lookup = {}

    drives = game_data.get("drives") or {}

    previous_drives = drives.get("previous") or []

    for drive in previous_drives:

        for play in drive.get("plays", []):

            play_id = play.get("id")

            if play_id is not None:
                lookup[str(play_id)] = play

    return lookup


# ============================================================
# TIMESTAMP HELPERS
# ============================================================

def parse_utc_timestamp(value):
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
    if clock_value is None:
        return None

    try:
        minutes, seconds = str(clock_value).split(":")
        return int(minutes) * 60 + int(seconds)

    except (ValueError, TypeError):
        return None


def reconstruct_game_clock_timestamp(
    game_kickoff,
    period_number,
    clock_value,
):
    """
    Approximate timestamp based purely on game clock.

    This is NOT used as the primary timestamp anymore.
    It is retained so we can compare it against ESPN wallclock.
    """

    kickoff = parse_utc_timestamp(game_kickoff)

    if kickoff is None:
        return None

    remaining = parse_game_clock(clock_value)

    if remaining is None:
        return None

    try:
        period_number = int(period_number)
    except (TypeError, ValueError):
        period_number = 1

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
# ESPN PROBABILITY SERIES
# ============================================================

def build_espn_probability_series(
    game_data,
    game_kickoff,
):
    """
    Build timestamped ESPN win-probability observations.

    IMPORTANT:
    Only use the actual wallclock timestamp from the full ESPN
    play object. Never reconstruct timestamps from the game clock.

    If an ESPN probability observation does not have a corresponding
    play with a valid wallclock, that observation is discarded.
    """

    winprobability = game_data.get(
        "winprobability",
        [],
    )

    play_lookup = build_play_lookup(
        game_data
    )

    series = []

    for entry in winprobability:

        if not isinstance(entry, dict):
            continue

        # ----------------------------------------------------
        # PLAY
        # ----------------------------------------------------

        play_id = entry.get("playId")

        play = None

        if play_id is not None:
            play = play_lookup.get(
                str(play_id)
            )

        # Do NOT fall back to reconstructed timestamps.
        if play is None:
            continue

        # ----------------------------------------------------
        # PROBABILITY
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
        except (TypeError, ValueError):
            continue

        # ESPN may return either 0-1 or 0-100.
        if home_probability > 1.0:
            home_probability /= 100.0

        home_probability = max(
            0.0,
            min(1.0, home_probability),
        )

        # ----------------------------------------------------
        # ACTUAL ESPN WALLCLOCK
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
        # PLAY INFORMATION
        # ----------------------------------------------------

        period = play.get(
            "period"
        ) or {}

        clock = play.get(
            "clock"
        ) or {}

        period_number = period.get(
            "number"
        )

        clock_display = clock.get(
            "displayValue"
        )

        # ----------------------------------------------------
        # STORE OBSERVATION
        # ----------------------------------------------------

        series.append({
            # This is ALWAYS the actual ESPN wallclock.
            "timestamp":
                timestamp,

            "wallclock":
                timestamp,

            "home_probability":
                home_probability,

            "away_probability":
                1.0 - home_probability,

            "play_id":
                play_id,

            "period":
                period_number,

            "clock":
                clock_display,

            "play_text":
                play.get("text"),
        })

    # --------------------------------------------------------
    # SORT BY ACTUAL ESPN WALLCLOCK
    # --------------------------------------------------------

    series.sort(
        key=lambda x: x["timestamp"]
    )

    return series


def latest_espn_observation(
    espn_series,
    target_timestamp,
):
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
# GAME LOOKUP
# ============================================================

def get_game_probability(game_id):

    data = get_game_data(game_id)

    winprobability = data.get(
        "winprobability"
    )

    if not winprobability:
        raise ValueError(
            f"No ESPN win probability available for game {game_id}"
        )

    first_probability = winprobability[0]

    home_probability = first_probability.get(
        "homeWinPercentage"
    )

    if home_probability is None:
        raise ValueError(
            f"Incomplete ESPN win probability data for game {game_id}"
        )

    home_probability = float(
        home_probability
    )

    return {
        "home": home_probability,
        "away": 1.0 - home_probability,
    }


def find_game(home_team, away_team, game_date):

    target_home = normalize_team_for_espn(
        home_team
    )

    target_away = normalize_team_for_espn(
        away_team
    )

    dates_to_search = [
        game_date,
        game_date + timedelta(days=1),
    ]

    for date in dates_to_search:

        try:
            scoreboard = get_scoreboard(
                date
            )

        except Exception as e:

            print(
                f"Could not get ESPN scoreboard for "
                f"{date.date()}: {e}"
            )

            continue

        for event in scoreboard.get(
            "events",
            [],
        ):

            try:

                competition = event[
                    "competitions"
                ][0]

                competitors = competition[
                    "competitors"
                ]

                home = next(
                    team
                    for team in competitors
                    if team["homeAway"] == "home"
                )

                away = next(
                    team
                    for team in competitors
                    if team["homeAway"] == "away"
                )

                espn_home = home[
                    "team"
                ]["abbreviation"]

                espn_away = away[
                    "team"
                ]["abbreviation"]

            except (
                KeyError,
                StopIteration,
            ):
                continue

            if (
                espn_home.upper()
                != target_home
                or
                espn_away.upper()
                != target_away
            ):
                continue

            game_id = event["id"]

            probabilities = (
                get_game_probability(
                    game_id
                )
            )

            home_score = None
            away_score = None
            home_winner = None

            try:

                home_score = int(
                    home.get("score")
                )

                away_score = int(
                    away.get("score")
                )

                home_winner = bool(
                    home.get("winner")
                )

            except (
                TypeError,
                ValueError,
            ):
                pass

            return {

                "id":
                    game_id,

                "start_time":
                    event["date"],

                "home_team":
                    espn_home,

                "away_team":
                    espn_away,

                "home_probability":
                    probabilities["home"],

                "away_probability":
                    probabilities["away"],

                "home_score":
                    home_score,

                "away_score":
                    away_score,

                "home_winner":
                    home_winner,
            }

    raise ValueError(
        f"Could not find ESPN game for "
        f"{away_team} @ {home_team} on "
        f"{game_date.date()}"
    )


def get_upcoming_games(days=30):

    games = []
    seen_ids = set()

    for i in range(days):

        date = (
            REGULAR_SEASON_START
            + timedelta(days=i)
        )

        try:

            data = get_scoreboard(
                date
            )

        except Exception as e:

            print(
                f"Could not get scoreboard for "
                f"{date.date()}: {e}"
            )

            continue

        for event in data.get(
            "events",
            [],
        ):

            game_id = event["id"]

            if game_id in seen_ids:
                continue

            seen_ids.add(game_id)

            competition = event[
                "competitions"
            ][0]

            competitors = competition[
                "competitors"
            ]

            try:

                home_team = next(
                    team
                    for team in competitors
                    if team["homeAway"] == "home"
                )

                away_team = next(
                    team
                    for team in competitors
                    if team["homeAway"] == "away"
                )

                probabilities = (
                    get_game_probability(
                        game_id
                    )
                )

            except Exception as e:

                print(
                    f"Could not process game "
                    f"{game_id}: {e}"
                )

                continue

            games.append({

                "id":
                    game_id,

                "start_time":
                    event["date"],

                "home_team":
                    home_team["team"][
                        "abbreviation"
                    ],

                "away_team":
                    away_team["team"][
                        "abbreviation"
                    ],

                "home_probability":
                    probabilities["home"],

                "away_probability":
                    probabilities["away"],
            })

    games.sort(
        key=lambda game:
        game["start_time"]
    )

    return games