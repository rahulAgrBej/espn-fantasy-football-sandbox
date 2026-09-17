"""The five scheduled jobs -- handoff §7's weekly schedule. Nothing here is
wired to cron (see the README's commented crontab, same "opt in
explicitly" stance as the nflverse layer) -- each function is one
CLI-triggered, budget-bounded pull. The one thing that makes this
different from every nflverse/sleeper command: re-running one of these
spends real credits. There is no `--refresh`-style "just check again" here.

Event selection for the props jobs (handoff §8) walks the *current* ESPN
roster via `rosters.rosters_frame` + `cache.ttl_for`, maps each rostered
player's `pro_team` to an event id from the free `/events` call, and pulls
props only for games that hold one of those players -- not all 16. There
is no waiver-target list anywhere else in this codebase yet, so "on our
own roster" is the whole selection rule for now; extending it to flagged
waiver targets is future work, not a silent gap, since `unmatched` +
`stale` are always surfaced.
"""

import datetime as dt

import pandas as pd

from .. import config
from ..cache import ttl_for
from ..extract import rosters
from ..extract import settings as espn_settings
from ..names import normalize_name, normalize_team
from .. import weeks
from . import ledger, store
from .client import OddsClient
from .markets import markets_for_event

WEDNESDAY = 2  # datetime.weekday(): Monday=0 ... props open Wed-Thu.


def _now(clock):
    return (clock or (lambda: dt.datetime.now(dt.timezone.utc)))()


def _flatten_featured(payload, captured_at, week):
    """A /odds (featured) response -> tidy rows for team_totals.parquet.
    Only "spreads" outcomes carry a real team name; "totals" outcomes are
    Over/Under and get `team=None` so consensus_line groups every book's
    Over/Under pair for that event together instead of splitting on the
    outcome label."""
    rows = []
    for event in payload or []:
        event_id = event.get("id")
        for book in event.get("bookmakers") or []:
            book_key = book.get("key")
            for market in book.get("markets") or []:
                market_key = market.get("key")
                for outcome in market.get("outcomes") or []:
                    team = normalize_team(outcome.get("name")) if market_key == "spreads" else None
                    rows.append({
                        "captured_at": captured_at, "week": week, "event_id": event_id, "team": team,
                        "market": market_key, "book": book_key, "outcome_name": outcome.get("name"),
                        "price": outcome.get("price"), "point": outcome.get("point"),
                    })
    columns = ["captured_at", "week", "event_id", "team", "market", "book", "outcome_name", "price", "point"]
    return pd.DataFrame(rows, columns=columns)


def _flatten_event_odds(payload, captured_at, week, team_by_name):
    """A /events/{id}/odds response -> tidy rows for player_props.parquet.
    The API gives a player's full name (`description`) and nothing else --
    `team_by_name` (built from our own roster walk) fills in `team` for the
    rows it can; everything else gets `team=None` and is resolved on name
    alone in ids.resolve, or left `unmatched`."""
    event_id = payload.get("id")
    rows = []
    for book in payload.get("bookmakers") or []:
        book_key = book.get("key")
        for market in book.get("markets") or []:
            market_key = market.get("key")
            for outcome in market.get("outcomes") or []:
                player_name = outcome.get("description")
                team = team_by_name.get(normalize_name(player_name)) if player_name else None
                rows.append({
                    "captured_at": captured_at, "week": week, "event_id": event_id,
                    "player_name": player_name, "team": team, "market": market_key, "book": book_key,
                    "outcome_name": outcome.get("name"), "price": outcome.get("price"),
                    "point": outcome.get("point"),
                })
    columns = ["captured_at", "week", "event_id", "player_name", "team", "market", "book", "outcome_name", "price", "point"]
    return pd.DataFrame(rows, columns=columns)


def _events_by_team(events_payload, window=None):
    """ESPN team abbreviation -> the id of that team's **next** event, from a
    free /events response.

    `/events` returns every upcoming event, which for the NFL is two or more
    weeks of them at once. This used to assign `index[team] = event_id`
    unconditionally while walking that list, so the *last* event mentioning a
    team won -- the furthest-out one. Every props pull therefore asked for
    player markets on games 8-12 days away, which no book has posted yet:
    10 events fetched, 0 bookmakers on every one, 0 rows written, on every
    run since the layer shipped (Observed 2026-09-17 -- the payloads cached
    under `data/raw/odds/**/event_odds/` that day all carry `commence_time`
    2026-09-25..29, while week 2's games were 09-17..09-21, and
    `player_props.parquet` had never been written at all).

    `window` is the fantasy week's `(start_et, end_et)` from
    `weeks.week_window`. It drops events outside the week entirely, which is
    what stops a team on bye from mapping to next week's game. When the
    calendar isn't on disk `week_window` returns None, and picking the
    earliest upcoming event per team is the correct fallback on its own --
    it is the half of this that fixes the bug.
    """
    best = {}
    for event in events_payload or []:
        commence = _commence_dt(event.get("commence_time"))
        if window is not None and commence is not None:
            start, end = window
            if not (start <= commence < end):
                continue
        for side in ("home_team", "away_team"):
            team = normalize_team(event.get(side))
            if team is None:
                continue
            prior = best.get(team)
            # `commence is None` sorts last: an event with no parseable
            # kickoff is a fallback, never preferred over a dated one.
            if prior is None or _earlier(commence, prior[0]):
                best[team] = (commence, event.get("id"))
    return {team: event_id for team, (_, event_id) in best.items()}


def _commence_dt(value):
    """The Odds API's ISO-8601 `commence_time` ("...Z") as an aware datetime,
    or None when absent or unparseable. Compared against `week_window`'s ET
    bounds, so it must be timezone-aware -- a naive value would raise on the
    comparison rather than merely sort oddly."""
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _earlier(candidate, incumbent):
    if candidate is None:
        return False
    return incumbent is None or candidate < incumbent


def decision_events(espn_client, events_payload, team_id, week=None, commence_after=None, event_ids=None):
    """event_id -> (positions on our roster in that game, {name: team}).
    `commence_after` (handoff §8's undecided-slot filter for pre_lock)
    drops any event whose kickoff has already passed. `event_ids`, when
    given (the CLI's `--events`), narrows the result to that explicit set
    -- it only ever restricts the roster-driven selection, never adds to it."""
    week = week or espn_client.current_scoring_period()
    payload = espn_client.get_league(
        ["mRoster", "mTeam"], scoring_period=week, ttl=ttl_for(week, espn_client.current_scoring_period())
    )
    roster_df = rosters.rosters_frame(payload, season=espn_client.season, week=week)
    team_roster = roster_df[roster_df["team_id"] == team_id]

    # Restrict to this fantasy week's games. Without it a team on bye maps to
    # its next game, a week out, and we buy props nobody has posted yet.
    event_by_team = _events_by_team(events_payload, window=weeks.week_window(espn_client.season, week))
    commence_by_event = {e.get("id"): e.get("commence_time") for e in events_payload or []}

    by_event = {}
    team_by_name = {}
    for _, row in team_roster.iterrows():
        event_id = event_by_team.get(row["pro_team"])
        if event_id is None:
            continue
        if commence_after is not None and (commence_by_event.get(event_id) or "") <= commence_after:
            continue
        if event_ids is not None and event_id not in event_ids:
            continue
        by_event.setdefault(event_id, []).append(row["position"])
        team_by_name[normalize_name(row["player_name"])] = row["pro_team"]
    return by_event, team_by_name


def _pull_props(client, espn_client, team_id, events_payload, week, now, priority="normal", commence_after=None, event_ids=None):
    by_event, team_by_name = decision_events(
        espn_client, events_payload, team_id, week=week, commence_after=commence_after, event_ids=event_ids
    )
    captured_at = now.isoformat()
    frames = []
    for event_id, positions in by_event.items():
        markets = markets_for_event(positions)
        if not markets:
            continue  # e.g. a game holding only our D/ST, which has no prop market
        payload = client.get_event_odds(event_id, sorted(markets), priority=priority)
        frames.append(_flatten_event_odds(payload, captured_at, week, team_by_name))
    tidy = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if not tidy.empty:
        store.append_snapshot(tidy, config.ODDS_PROPS, store.PROPS_DEDUPE_KEYS)
    return len(by_event), tidy


def slate_context(espn_client, team_id=None, con=None, clock=None, week=None):
    """Tue 09:00 ET. `/events` (free) + `/odds?markets=spreads,totals` (2
    credits) -- the highest-value call in the whole layer: it covers the
    entire slate and is the sole basis for implied team totals. Also
    snapshots league scoring from ESPN's own settings, since we already
    hold an EspnClient here and every downstream points conversion needs it.
    """
    con = con or ledger.open_db()
    now = _now(clock)
    week = week or espn_client.current_scoring_period()
    job_run_id = ledger.start_run(con, "slate_context", run_budget=4)
    before = ledger.state(con)["spent"]

    try:
        client = OddsClient(con=con, job_run_id=job_run_id, clock=clock)
        client.assert_sport_live()
        events = client.get_events()
        featured = client.get_featured_odds(["spreads", "totals"], priority="normal")
    except ledger.BudgetExceeded as exc:
        ledger.finish_run(con, job_run_id, aborted_reason=str(exc))
        store.write_last_run("slate_context", credits_spent=0, stale=True, reason=str(exc), ran_at=now.timestamp())
        raise

    base_payload = espn_client.get_league(
        ["mSettings", "mTeam", "mStandings"], ttl=ttl_for(None, espn_client.current_scoring_period())
    )
    store.write_league_scoring(espn_settings.scoring_frame(base_payload, espn_client.season).to_dict("records"))

    stale, reason = False, None
    if not featured:
        stale, reason = True, "no odds returned; markets likely not open yet"
    else:
        store.append_snapshot(_flatten_featured(featured, now.isoformat(), week), config.ODDS_TEAM_TOTALS, store.TOTALS_DEDUPE_KEYS)

    credits_spent = ledger.state(con)["spent"] - before
    store.write_last_run("slate_context", credits_spent=credits_spent, stale=stale, reason=reason, ran_at=now.timestamp())
    return {"events": len(events), "featured_events": len(featured), "credits_spent": credits_spent, "stale": stale}


def props_primary(espn_client, team_id=None, con=None, clock=None, week=None, force=False, today=None, event_ids=None):
    """Thu 10:00 ET. `/events/{id}/odds` for decision-relevant games only.
    Refuses to run before Wednesday -- props open Wed-Thu, and an earlier
    call is free but returns nothing, burning the job slot for no reason.
    `--force` overrides only this weekday check, never the budget.
    """
    team_id = team_id if team_id is not None else config.TEAM_ID
    today = today or dt.date.today()
    if not force and today.weekday() < WEDNESDAY:
        reason = "refusing to run before Wednesday -- props open Wed-Thu; an earlier pull is free but empty"
        store.write_last_run("props_primary", credits_spent=0, stale=True, reason=reason)
        return {"skipped": True, "reason": reason}

    con = con or ledger.open_db()
    now = _now(clock)
    week = week or espn_client.current_scoring_period()
    job_run_id = ledger.start_run(con, "props_primary", run_budget=40)
    before = ledger.state(con)["spent"]

    try:
        client = OddsClient(con=con, job_run_id=job_run_id, clock=clock)
        events = client.get_events()
        events_pulled, tidy = _pull_props(client, espn_client, team_id, events, week, now, event_ids=event_ids)
    except ledger.BudgetExceeded as exc:
        ledger.finish_run(con, job_run_id, aborted_reason=str(exc))
        store.write_last_run("props_primary", credits_spent=0, stale=True, reason=str(exc), ran_at=now.timestamp())
        raise

    stale = tidy.empty
    reason = "no props returned; markets likely not open yet" if stale else None
    credits_spent = ledger.state(con)["spent"] - before
    store.write_last_run("props_primary", credits_spent=credits_spent, stale=stale, reason=reason, ran_at=now.timestamp())
    return {"events_pulled": events_pulled, "prop_rows": len(tidy), "credits_spent": credits_spent, "stale": stale}


def line_movement(espn_client, con=None, clock=None, week=None):
    """Fri 10:00 ET. `/odds?markets=spreads,totals` again -- the delta
    against Tuesday's slate_context capture is the line-movement signal;
    both captures live side by side in team_totals.parquet forever."""
    con = con or ledger.open_db()
    now = _now(clock)
    week = week or espn_client.current_scoring_period()
    job_run_id = ledger.start_run(con, "line_movement", run_budget=4)
    before = ledger.state(con)["spent"]

    try:
        client = OddsClient(con=con, job_run_id=job_run_id, clock=clock)
        featured = client.get_featured_odds(["spreads", "totals"], priority="normal")
    except ledger.BudgetExceeded as exc:
        ledger.finish_run(con, job_run_id, aborted_reason=str(exc))
        store.write_last_run("line_movement", credits_spent=0, stale=True, reason=str(exc), ran_at=now.timestamp())
        raise

    stale, reason = False, None
    if not featured:
        stale, reason = True, "no odds returned; markets likely not open yet"
    else:
        store.append_snapshot(_flatten_featured(featured, now.isoformat(), week), config.ODDS_TEAM_TOTALS, store.TOTALS_DEDUPE_KEYS)

    credits_spent = ledger.state(con)["spent"] - before
    store.write_last_run("line_movement", credits_spent=credits_spent, stale=stale, reason=reason, ran_at=now.timestamp())
    return {"featured_events": len(featured), "credits_spent": credits_spent, "stale": stale}


def pre_lock(espn_client, team_id=None, con=None, clock=None, week=None, event_ids=None):
    """Sun 10:30 ET. `priority="critical"` -- the only job allowed to draw
    the RESERVE down to zero. Featured odds for one last line read, plus
    props for undecided slots only: any event whose kickoff has already
    passed is excluded, since there is nothing left to decide about it."""
    team_id = team_id if team_id is not None else config.TEAM_ID
    con = con or ledger.open_db()
    now = _now(clock)
    week = week or espn_client.current_scoring_period()
    job_run_id = ledger.start_run(con, "pre_lock", run_budget=25)
    before = ledger.state(con)["spent"]

    try:
        client = OddsClient(con=con, job_run_id=job_run_id, clock=clock)
        events = client.get_events()
        featured = client.get_featured_odds(["spreads", "totals"], priority="critical")
        if featured:
            store.append_snapshot(_flatten_featured(featured, now.isoformat(), week), config.ODDS_TEAM_TOTALS, store.TOTALS_DEDUPE_KEYS)
        events_pulled, tidy = _pull_props(
            client, espn_client, team_id, events, week, now,
            priority="critical", commence_after=now.isoformat(), event_ids=event_ids,
        )
    except ledger.BudgetExceeded as exc:
        ledger.finish_run(con, job_run_id, aborted_reason=str(exc))
        store.write_last_run("pre_lock", credits_spent=0, stale=True, reason=str(exc), ran_at=now.timestamp())
        raise

    stale = not featured and tidy.empty
    reason = "no odds returned for any undecided slot" if stale else None
    credits_spent = ledger.state(con)["spent"] - before
    store.write_last_run("pre_lock", credits_spent=credits_spent, stale=stale, reason=reason, ran_at=now.timestamp())
    return {
        "featured_events": len(featured), "undecided_events_pulled": events_pulled,
        "prop_rows": len(tidy), "credits_spent": credits_spent, "stale": stale,
    }


def results(con=None, clock=None, days_from=3):
    """Mon 09:00 ET. `/scores?daysFrom=3` (cost 2, not 1) -- we need
    completed games, not just live ones; stat corrections land Tue/Wed same
    as the nflverse layer, so Monday's read is the first canonical one."""
    con = con or ledger.open_db()
    now = _now(clock)
    job_run_id = ledger.start_run(con, "results", run_budget=4)
    before = ledger.state(con)["spent"]

    try:
        client = OddsClient(con=con, job_run_id=job_run_id, clock=clock)
        scores = client.get_scores(days_from=days_from, priority="normal")
    except ledger.BudgetExceeded as exc:
        ledger.finish_run(con, job_run_id, aborted_reason=str(exc))
        store.write_last_run("results", credits_spent=0, stale=True, reason=str(exc), ran_at=now.timestamp())
        raise

    stale = not scores
    reason = "no completed games returned" if stale else None
    credits_spent = ledger.state(con)["spent"] - before
    store.write_last_run("results", credits_spent=credits_spent, stale=stale, reason=reason, ran_at=now.timestamp())
    return {"games": len(scores), "credits_spent": credits_spent, "stale": stale}


def estimate_cost(job_name, espn_client=None, team_id=None, week=None, today=None, con=None, clock=None, event_ids=None):
    """The cost `job_name` would incur if run right now, computed without
    spending a credit -- `get_events()` is free and never touches the
    ledger, so this can walk the same roster-driven event selection the
    real job would use and still leave the ledger untouched. Backs
    `odds --dry-run`, which must never itself move a credit.
    """
    if job_name == "results":
        return 2  # daysFrom cost
    if job_name in ("slate", "line_movement"):
        return 2  # spreads + totals, 1 region
    if job_name not in ("props", "pre_lock"):
        raise ValueError(f"unknown job {job_name!r}")

    if job_name == "props":
        today = today or dt.date.today()
        if today.weekday() < WEDNESDAY:
            return 0

    team_id = team_id if team_id is not None else config.TEAM_ID
    client = OddsClient(con=con or ledger.open_db(), job_run_id="dry-run", clock=clock)
    events = client.get_events()
    commence_after = _now(clock).isoformat() if job_name == "pre_lock" else None
    by_event, _team_by_name = decision_events(
        espn_client, events, team_id, week=week, commence_after=commence_after, event_ids=event_ids
    )
    props_cost = sum(len(markets_for_event(p)) for p in by_event.values() if markets_for_event(p))
    return props_cost + (2 if job_name == "pre_lock" else 0)  # pre_lock also re-reads featured odds


JOBS = {
    "slate": slate_context,
    "props": props_primary,
    "line_movement": line_movement,
    "pre_lock": pre_lock,
    "results": results,
}
