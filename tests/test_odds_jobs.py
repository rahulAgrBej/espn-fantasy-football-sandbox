"""espn_ff.odds.jobs -- orchestration only. A fake OddsClient stand-in
(never touches the network or the ledger) isolates event-selection,
weekday refusal, and store/last_run behaviour; the ledger and client's own
guard/reconcile contracts are covered in test_odds_ledger.py and
test_odds_client.py."""

import datetime as dt

import pandas as pd
import pytest

from espn_ff.odds import jobs, ledger, store


class _FakeEspnClient:
    def __init__(self, roster_payload, season=2026, week=3):
        self.roster_payload = roster_payload
        self.season = season
        self._week = week

    def current_scoring_period(self):
        return self._week

    def get_league(self, views, scoring_period=None, ttl=None):
        return self.roster_payload


class _FakeOddsClient:
    def __init__(self, events=None, featured=None, event_odds_by_id=None, scores=None, **kwargs):
        self._events = events if events is not None else []
        self._featured = featured if featured is not None else []
        self._event_odds_by_id = event_odds_by_id or {}
        self._scores = scores if scores is not None else []
        self.event_odds_calls = []  # (event_id, markets, priority)
        self.featured_calls = []

    def assert_sport_live(self):
        pass

    def get_events(self):
        return self._events

    def get_featured_odds(self, markets, priority="normal"):
        self.featured_calls.append((tuple(markets), priority))
        return self._featured

    def get_event_odds(self, event_id, markets, priority="normal"):
        self.event_odds_calls.append((event_id, tuple(markets), priority))
        return self._event_odds_by_id.get(event_id, {"id": event_id, "bookmakers": []})

    def get_scores(self, days_from=None, priority="normal"):
        return self._scores


def _player_entry(player_id, name, position_id, pro_team_id, slot_id=2):
    return {
        "lineupSlotId": slot_id,
        "acquisitionType": "DRAFT",
        "playerPoolEntry": {"player": {"id": player_id, "fullName": name, "defaultPositionId": position_id, "proTeamId": pro_team_id}},
    }


def _roster_payload(team_id=5):
    return {
        "teams": [
            {
                "id": team_id,
                "name": "Test Team",
                "roster": {
                    "entries": [
                        _player_entry(100, "Jayden Reed", 3, 9),   # WR, GB
                        _player_entry(200, "Patrick Mahomes", 1, 12),  # QB, KC
                    ]
                },
            }
        ]
    }


def _events_payload():
    return [
        {"id": "evt_gb_min", "commence_time": "2026-09-21T17:00:00Z", "home_team": "Minnesota Vikings", "away_team": "Green Bay Packers"},
        {"id": "evt_kc_bal", "commence_time": "2026-09-21T20:25:00Z", "home_team": "Baltimore Ravens", "away_team": "Kansas City Chiefs"},
        {"id": "evt_unrelated", "commence_time": "2026-09-21T13:00:00Z", "home_team": "Detroit Lions", "away_team": "Chicago Bears"},
    ]


def _featured_payload():
    return [
        {
            "id": "evt_gb_min", "commence_time": "2026-09-21T17:00:00Z",
            "home_team": "Minnesota Vikings", "away_team": "Green Bay Packers",
            "bookmakers": [{"key": "draftkings", "markets": [
                {"key": "spreads", "outcomes": [{"name": "Minnesota Vikings", "price": -130, "point": -2.5}, {"name": "Green Bay Packers", "price": 110, "point": 2.5}]},
                {"key": "totals", "outcomes": [{"name": "Over", "price": -110, "point": 44.5}, {"name": "Under", "price": -110, "point": 44.5}]},
            ]}],
        }
    ]


@pytest.fixture
def odds_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(store.config, "ODDS_DIR", tmp_path)
    monkeypatch.setattr(store.config, "ODDS_RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(store.config, "ODDS_PROPS", tmp_path / "player_props.parquet")
    monkeypatch.setattr(store.config, "ODDS_TEAM_TOTALS", tmp_path / "team_totals.parquet")
    monkeypatch.setattr(store.config, "ODDS_SCORING", tmp_path / "league_scoring.json")
    monkeypatch.delenv("ODDS_QUOTA_RESET_DAY", raising=False)
    return tmp_path


@pytest.fixture
def con(tmp_path):
    return ledger.open_db(tmp_path / "ledger.db")


def _patch_client(monkeypatch, fake):
    monkeypatch.setattr(jobs, "OddsClient", lambda *a, **kw: fake)


def test_slate_context_writes_team_totals_and_league_scoring(con, odds_paths, monkeypatch):
    fake = _FakeOddsClient(events=_events_payload(), featured=_featured_payload())
    _patch_client(monkeypatch, fake)
    espn_client = _FakeEspnClient(_roster_payload())

    result = jobs.slate_context(espn_client, con=con)

    assert result["stale"] is False
    totals = pd.read_parquet(store.config.ODDS_TEAM_TOTALS)
    assert set(totals["market"]) == {"spreads", "totals"}
    assert store.config.ODDS_SCORING.exists()
    last_run = store.read_last_run("slate_context")
    assert last_run["stale"] is False


def test_slate_context_marks_stale_on_empty_featured_response(con, odds_paths, monkeypatch):
    fake = _FakeOddsClient(events=_events_payload(), featured=[])
    _patch_client(monkeypatch, fake)
    espn_client = _FakeEspnClient(_roster_payload())

    result = jobs.slate_context(espn_client, con=con)

    assert result["stale"] is True
    assert not store.config.ODDS_TEAM_TOTALS.exists()
    assert "not open yet" in store.read_last_run("slate_context")["reason"]


def test_props_primary_refuses_to_run_before_wednesday_without_force(con, odds_paths, monkeypatch):
    fake = _FakeOddsClient(events=_events_payload())
    _patch_client(monkeypatch, fake)
    espn_client = _FakeEspnClient(_roster_payload())
    monday = dt.date(2026, 9, 14)

    result = jobs.props_primary(espn_client, con=con, today=monday)

    assert result["skipped"] is True
    assert fake.event_odds_calls == []


def test_props_primary_force_overrides_the_weekday_check(con, odds_paths, monkeypatch):
    fake = _FakeOddsClient(events=_events_payload(), event_odds_by_id={})
    _patch_client(monkeypatch, fake)
    espn_client = _FakeEspnClient(_roster_payload())
    monday = dt.date(2026, 9, 14)

    result = jobs.props_primary(espn_client, con=con, today=monday, force=True)

    assert "skipped" not in result


def test_props_primary_pulls_only_events_holding_our_own_roster(con, odds_paths, monkeypatch):
    fake = _FakeOddsClient(events=_events_payload())
    _patch_client(monkeypatch, fake)
    espn_client = _FakeEspnClient(_roster_payload())
    thursday = dt.date(2026, 9, 17)

    jobs.props_primary(espn_client, con=con, today=thursday)

    called_events = {event_id for event_id, _markets, _priority in fake.event_odds_calls}
    assert called_events == {"evt_gb_min", "evt_kc_bal"}
    assert "evt_unrelated" not in called_events


def test_props_primary_requests_the_position_specific_market_set(con, odds_paths, monkeypatch):
    """Jayden Reed (WR) is the only rostered player in evt_gb_min -- the
    call for that event must carry exactly the WR market set, not QB's."""
    fake = _FakeOddsClient(events=_events_payload())
    _patch_client(monkeypatch, fake)
    espn_client = _FakeEspnClient(_roster_payload())
    thursday = dt.date(2026, 9, 17)

    jobs.props_primary(espn_client, con=con, today=thursday)

    markets_by_event = {event_id: set(markets) for event_id, markets, _priority in fake.event_odds_calls}
    assert markets_by_event["evt_gb_min"] == {"player_reception_yds", "player_receptions", "player_anytime_td"}
    assert markets_by_event["evt_kc_bal"] == {"player_pass_yds", "player_pass_tds"}


def test_props_primary_marks_stale_when_no_prop_rows_come_back(con, odds_paths, monkeypatch):
    fake = _FakeOddsClient(events=_events_payload(), event_odds_by_id={
        "evt_gb_min": {"id": "evt_gb_min", "bookmakers": []},
        "evt_kc_bal": {"id": "evt_kc_bal", "bookmakers": []},
    })
    _patch_client(monkeypatch, fake)
    espn_client = _FakeEspnClient(_roster_payload())
    thursday = dt.date(2026, 9, 17)

    result = jobs.props_primary(espn_client, con=con, today=thursday)

    assert result["stale"] is True
    assert store.read_last_run("props_primary")["stale"] is True


def test_pre_lock_excludes_events_whose_kickoff_has_already_passed(con, odds_paths, monkeypatch):
    fake = _FakeOddsClient(events=_events_payload(), featured=_featured_payload())
    _patch_client(monkeypatch, fake)
    espn_client = _FakeEspnClient(_roster_payload())
    # "now" is after evt_gb_min's kickoff but before evt_kc_bal's.
    clock = lambda: dt.datetime(2026, 9, 21, 18, 0, 0, tzinfo=dt.timezone.utc)

    jobs.pre_lock(espn_client, con=con, clock=clock)

    called_events = {event_id for event_id, _markets, _priority in fake.event_odds_calls}
    assert called_events == {"evt_kc_bal"}


def test_pre_lock_uses_critical_priority_for_featured_odds(con, odds_paths, monkeypatch):
    fake = _FakeOddsClient(events=_events_payload(), featured=_featured_payload())
    _patch_client(monkeypatch, fake)
    espn_client = _FakeEspnClient(_roster_payload())
    clock = lambda: dt.datetime(2026, 9, 20, 0, 0, 0, tzinfo=dt.timezone.utc)

    jobs.pre_lock(espn_client, con=con, clock=clock)

    assert fake.featured_calls[0][1] == "critical"


def test_results_marks_stale_on_empty_scores(con, odds_paths, monkeypatch):
    fake = _FakeOddsClient(scores=[])
    _patch_client(monkeypatch, fake)

    result = jobs.results(con=con)

    assert result["stale"] is True
    assert store.read_last_run("results")["stale"] is True


def test_results_reports_completed_games(con, odds_paths, monkeypatch):
    fake = _FakeOddsClient(scores=[{"id": "e1", "completed": True, "home_team": "A", "away_team": "B"}])
    _patch_client(monkeypatch, fake)

    result = jobs.results(con=con)

    assert result["games"] == 1
    assert result["stale"] is False


def test_estimate_cost_never_touches_the_ledger(con, odds_paths, monkeypatch):
    fake = _FakeOddsClient(events=_events_payload())
    _patch_client(monkeypatch, fake)
    espn_client = _FakeEspnClient(_roster_payload())
    thursday = dt.date(2026, 9, 17)

    est = jobs.estimate_cost("props", espn_client=espn_client, con=con, today=thursday)

    assert est == 3 + 2  # evt_gb_min (WR: 3 markets) + evt_kc_bal (QB: 2 markets)
    assert con.execute("SELECT COUNT(*) AS n FROM credit_ledger_entry").fetchone()["n"] == 0


def test_estimate_cost_for_props_is_zero_before_wednesday(con, odds_paths, monkeypatch):
    fake = _FakeOddsClient(events=_events_payload())
    _patch_client(monkeypatch, fake)
    monday = dt.date(2026, 9, 14)

    assert jobs.estimate_cost("props", espn_client=_FakeEspnClient(_roster_payload()), con=con, today=monday) == 0


def test_estimate_cost_for_slate_and_results_is_flat(con, odds_paths):
    assert jobs.estimate_cost("slate") == 2
    assert jobs.estimate_cost("results") == 2


def test_events_override_narrows_props_pull_to_the_given_ids(con, odds_paths, monkeypatch):
    fake = _FakeOddsClient(events=_events_payload())
    _patch_client(monkeypatch, fake)
    espn_client = _FakeEspnClient(_roster_payload())
    thursday = dt.date(2026, 9, 17)

    jobs.props_primary(espn_client, con=con, today=thursday, event_ids={"evt_gb_min"})

    called_events = {event_id for event_id, _markets, _priority in fake.event_odds_calls}
    assert called_events == {"evt_gb_min"}
