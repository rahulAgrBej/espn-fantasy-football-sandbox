"""The Odds API betting-market layer.

ESPN, Sleeper and nflverse all describe *what already happened* or *who can
play*. None of them carries a forward-looking, market-priced projection --
which is what the start/sit question actually needs. This package fills
that gap, and differs from the other three in one way that drives its whole
design: **it is metered.** 500 credits per billing period, free tier, no
historical endpoint. The other three feeds can all be re-fetched for free
when in doubt; here a careless re-run costs money we cannot get back, and a
missed window costs data we can never buy back -- `/v4/historical/*` is
paid-tier only, so the only odds history this project will ever have is
what it saves at fetch time.

Boundaries mostly mirror espn_ff/nflverse/: `client.py` talks HTTP only,
`store.py` owns disk and freshness, `markets.py` is the schema and cost
contract, `ids.py` is the name-based join to ESPN, and `projections.py` is
the pure offline derive. The one addition the other layers don't need is
`ledger.py`, a persistent, transactional credit ledger that every guarded
request must pass through -- built first, and with no bypass parameter
anywhere in this package. Because it has no dependencies of its own,
`OddsError` -- the base exception every other module in this package
raises -- is defined there rather than in `client.py`.
"""
