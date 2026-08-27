"""
Trading strategy for the Kalshi sports bot.

Compares ESPN's win probability against Kalshi's executable YES ask price.

Position sizing:
    < 5% edge   -> HOLD
    5% edge     -> $10
    6% edge     -> $12
    7% edge     -> $14
    8% edge     -> $16
    9% edge     -> $18
    10%+ edge   -> $20
"""

EDGE_THRESHOLD = 0.05
MAX_EDGE_FOR_SIZING = 0.10

MIN_BET_SIZE = 10.00
MAX_BET_SIZE = 20.00


def calculate_edge(espn_probability, kalshi_yes_ask):
    """Return the difference between ESPN probability and Kalshi price."""

    if espn_probability is None or kalshi_yes_ask is None:
        return None

    return espn_probability - kalshi_yes_ask


def calculate_bet_size(edge):
    """
    Calculate bet size based on edge.

    5% edge  -> $10
    10% edge -> $20
    >10%     -> $20

    Returns $0 if the edge is below the trading threshold.
    """

    if edge is None or edge < EDGE_THRESHOLD:
        return 0.00

    # Cap the edge used for sizing at 10%.
    capped_edge = min(edge, MAX_EDGE_FOR_SIZING)

    # Scale linearly from $10 at 5% to $20 at 10%.
    scale = (
        (capped_edge - EDGE_THRESHOLD)
        / (MAX_EDGE_FOR_SIZING - EDGE_THRESHOLD)
    )

    bet_size = MIN_BET_SIZE + (
        scale * (MAX_BET_SIZE - MIN_BET_SIZE)
    )

    # Round to the nearest dollar.
    return round(bet_size)


def evaluate_trade(
    team,
    espn_probability,
    kalshi_yes_bid,
    kalshi_yes_ask,
):
    """Evaluate whether we should buy a team's YES contract."""

    edge = calculate_edge(
        espn_probability,
        kalshi_yes_ask,
    )

    if edge is None:
        return {
            "team": team,
            "espn_probability": espn_probability,
            "kalshi_yes_bid": kalshi_yes_bid,
            "kalshi_yes_ask": kalshi_yes_ask,
            "edge": None,
            "action": "NO DATA",
            "bet_size": 0.00,
        }

    bet_size = calculate_bet_size(edge)

    if bet_size > 0:
        action = "BUY"
    else:
        action = "HOLD"

    return {
        "team": team,
        "espn_probability": espn_probability,
        "kalshi_yes_bid": kalshi_yes_bid,
        "kalshi_yes_ask": kalshi_yes_ask,
        "edge": edge,
        "action": action,
        "bet_size": bet_size,
    }


def print_trade_evaluation(result):
    """Print a human-readable trade evaluation."""

    team = result["team"]
    espn = result["espn_probability"]
    ask = result["kalshi_yes_ask"]
    edge = result["edge"]
    action = result["action"]
    bet_size = result["bet_size"]

    print(f"\n{team}")

    if espn is not None:
        print(f"  ESPN probability: {espn:.1%}")
    else:
        print("  ESPN probability: N/A")

    if ask is not None:
        print(f"  Kalshi YES ask:   {ask:.1%}")
    else:
        print("  Kalshi YES ask:   N/A")

    if edge is not None:
        print(f"  Edge:              {edge:+.1%}")
    else:
        print("  Edge:              N/A")

    print(f"  Action:            {action}")

    if bet_size > 0:
        print(f"  Bet size:          ${bet_size:.2f}")


if __name__ == "__main__":
    print("Testing position sizing:")

    test_edges = [0.03, 0.05, 0.06, 0.07, 0.08, 0.09, 0.10, 0.15]

    for edge in test_edges:
        bet_size = calculate_bet_size(edge)
        print(
            f"  Edge: {edge:.0%}"
            f"  -> Bet size: ${bet_size:.2f}"
        )

    print("\nTesting a trade:")

    result = evaluate_trade(
        team="TEST",
        espn_probability=0.30,
        kalshi_yes_bid=0.22,
        kalshi_yes_ask=0.23,
    )

    print_trade_evaluation(result)