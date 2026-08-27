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


# ---------------------------------------------------------
# TEAM ABBREVIATION MAPPING
# ---------------------------------------------------------

KALSHI_TO_ESPN = {
    "WAS": "WSH",
    "JAC": "JAX",
    "LA": "LAR",
}


def normalize_team_for_espn(team):
    """Convert a Kalshi team abbreviation to ESPN's abbreviation."""

    team = team.upper()

    return KALSHI_TO_ESPN.get(team, team)


def get_scoreboard(date):
    """Get the ESPN NFL scoreboard for a specific date."""

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
    """Retrieve the complete ESPN game package."""

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


def get_game_probability(game_id):
    """
    Get ESPN's historical pregame win probability.

    We intentionally use the first entry in the historical
    win-probability series, matching the previously tested
    implementation.
    """

    data = get_game_data(game_id)

    winprobability = data.get("winprobability")

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

    home_probability = float(home_probability)

    return {
        "home": home_probability,
        "away": 1.0 - home_probability,
    }


def find_game(home_team, away_team, game_date):
    """
    Find an ESPN game matching the Kalshi home/away teams
    and game date.

    Searches the requested date and the following date to
    protect against UTC date-boundary issues.
    """

    target_home = normalize_team_for_espn(home_team)
    target_away = normalize_team_for_espn(away_team)

    dates_to_search = [
        game_date,
        game_date + timedelta(days=1),
    ]

    for date in dates_to_search:

        try:
            scoreboard = get_scoreboard(date)

        except Exception as e:
            print(
                f"Could not get ESPN scoreboard for "
                f"{date.date()}: {e}"
            )
            continue

        for event in scoreboard.get("events", []):

            try:
                competition = event["competitions"][0]
                competitors = competition["competitors"]

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

                espn_home = home["team"]["abbreviation"]
                espn_away = away["team"]["abbreviation"]

            except (KeyError, StopIteration):
                continue

            if (
                espn_home.upper() != target_home
                or espn_away.upper() != target_away
            ):
                continue

            game_id = event["id"]

            probabilities = get_game_probability(game_id)

            home_score = None
            away_score = None
            home_winner = None

            try:
                home_score = int(home.get("score"))
                away_score = int(away.get("score"))
                home_winner = bool(home.get("winner"))

            except (TypeError, ValueError):
                pass

            return {
                "id": game_id,
                "start_time": event["date"],
                "home_team": espn_home,
                "away_team": espn_away,
                "home_probability": probabilities["home"],
                "away_probability": probabilities["away"],
                "home_score": home_score,
                "away_score": away_score,
                "home_winner": home_winner,
            }

    raise ValueError(
        f"Could not find ESPN game for "
        f"{away_team} @ {home_team} on "
        f"{game_date.date()}"
    )


def get_upcoming_games(days=30):
    """Get upcoming NFL regular-season games and ESPN probabilities."""

    games = []
    seen_ids = set()

    for i in range(days):

        date = REGULAR_SEASON_START + timedelta(days=i)

        try:
            data = get_scoreboard(date)

        except Exception as e:
            print(
                f"Could not get scoreboard for "
                f"{date.date()}: {e}"
            )
            continue

        for event in data.get("events", []):

            game_id = event["id"]

            if game_id in seen_ids:
                continue

            seen_ids.add(game_id)

            competition = event["competitions"][0]
            competitors = competition["competitors"]

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

                probabilities = get_game_probability(game_id)

            except Exception as e:
                print(
                    f"Could not process game "
                    f"{game_id}: {e}"
                )
                continue

            games.append({
                "id": game_id,
                "start_time": event["date"],
                "home_team": home_team["team"]["abbreviation"],
                "away_team": away_team["team"]["abbreviation"],
                "home_probability": probabilities["home"],
                "away_probability": probabilities["away"],
            })

    games.sort(
        key=lambda game: game["start_time"]
    )

    return games


if __name__ == "__main__":

    games = get_upcoming_games(days=30)

    print(
        f"Found {len(games)} NFL games\n"
    )

    for game in games:

        print(
            f"{game['id']}: "
            f"{game['away_team']} @ "
            f"{game['home_team']} "
            f"({game['start_time']})"
        )

        print(
            f"  {game['away_team']}: "
            f"{game['away_probability']:.1%}"
        )

        print(
            f"  {game['home_team']}: "
            f"{game['home_probability']:.1%}"
        )

        print()