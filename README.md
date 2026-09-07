# kalshi-sports-bot
betting algorithms for kalshi

## NFL delta paper trading

`src.paper_delta_trader` runs the existing five-minute delta strategy against
public ESPN win probabilities and Kalshi order books. It is intentionally
paper-only: it has no API credentials, no signing code, and no order endpoint.
Simulated fills and the recent quote buffer are written to
`runtime/paper_delta_state.json` (ignored by git).

Before the games start, verify the public feeds and local ledger with:

```bash
python -m src.paper_delta_trader --once
```

During live NFL games, leave it running from the repository root:

```bash
python -m src.paper_delta_trader --poll-seconds 15
```

The runner only considers games ESPN marks as in progress. A simulated fill
requires a 10% Kalshi YES-price movement over about five minutes, ESPN readings
within 30 seconds at both endpoints, and a delta edge above 5%. It records one
fill per ESPN end-play and team side, sized at $10 / $15 / $20 under the
strategy's existing rules.
