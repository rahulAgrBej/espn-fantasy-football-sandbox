"""The grounded news layer: how the roster is sliced, what the model is
told, and what happens to whatever it says back.

Every test here is offline and fixture-free by construction -- `ai/news.py`
is pure, takes DataFrames and a date, and never reads disk or a clock. The
reconciliation tests are the important ones: they are what turns "the model
returned some JSON" into "every rostered player is accounted for", and they
are the entire justification for storing news as structured objects rather
than as prose.
"""

import json
from datetime import date

import pandas as pd
import pytest

from espn_ff.ai import news

TODAY = date(2026, 9, 16)


def roster(rows):
    return pd.DataFrame(rows)


def player(player_id, name, slot, started, team="CHI", position="WR", status="ACTIVE", team_id=5,
           week=2):
    return {
        "season": 2026, "week": week, "team_id": team_id, "player_id": player_id,
        "player_name": name, "position": position, "pro_team": team,
        "lineup_slot": slot, "started": started, "injury_status": status,
    }


FULL_ROSTER = roster(
    [
        player(11, "Starter One", "QB", True, position="QB"),
        player(22, "Starter Two", "WR", True),
        player(33, "Bench Three", "Bench", False),
        player(44, "Injured Four", "IR", False, status="OUT"),
        player(55, "Rival Five", "TE", True, team_id=9),
        player(66, "Last Week", "QB", True, position="QB", week=1),
    ]
)


# --- slicing the roster ---------------------------------------------------


def test_the_three_groups_split_starters_bench_and_ir():
    groups = news.roster_groups(FULL_ROSTER, week=2, team_id=5)

    assert list(groups["starters"]["player_id"]) == [11, 22]
    assert list(groups["bench"]["player_id"]) == [33]
    assert list(groups["ir"]["player_id"]) == [44]


def test_ir_is_excluded_from_the_bench_not_merely_from_the_starters():
    """`started == False` is true for IR as well as the bench, so a filter
    that checks only `started` files every IR player under "bench".

    That would not raise anything. It would ask for "what changed this week"
    about a player who is on injured reserve, and the answer would look
    entirely plausible -- which is why this is pinned rather than trusted to
    the reader of `roster_groups`.
    """
    groups = news.roster_groups(FULL_ROSTER, week=2, team_id=5)

    assert 44 not in set(groups["bench"]["player_id"])
    assert set(groups["ir"]["player_id"]) == {44}


def test_the_groups_cover_our_team_this_week_and_nobody_else():
    groups = news.roster_groups(FULL_ROSTER, week=2, team_id=5)
    covered = {pid for group in groups.values() for pid in group["player_id"]}

    assert covered == {11, 22, 33, 44}, "a rival's player or another week leaked in"


@pytest.mark.parametrize(
    "frame",
    [pd.DataFrame(), pd.DataFrame({"week": [2], "team_id": [5]})],
    ids=["empty", "missing-columns"],
)
def test_an_unusable_export_yields_empty_groups_rather_than_raising(frame):
    """A cold runner whose restore-out found nothing must degrade to an
    explicit news_error in `summarize`, not crash a scheduled run."""
    groups = news.roster_groups(frame, week=2, team_id=5)

    assert set(groups) == set(news.GROUPS)
    assert all(group.empty for group in groups.values())


# --- what the model is told ----------------------------------------------


def test_the_instruction_anchors_latest_to_an_explicit_date():
    """Without a date the model calls a months-old item recent, and rule 2
    has nothing to bite on."""
    instruction = news.system_instruction("starters", 2026, 2, TODAY)

    assert "2026-09-16" in instruction
    assert "week 2" in instruction


def test_each_group_gets_its_own_brief():
    briefs = {group: news.system_instruction(group, 2026, 2, TODAY) for group in news.GROUPS}

    assert "starting lineup" in briefs["starters"]
    assert "bench" in briefs["bench"]
    assert "injured reserve" in briefs["ir"]
    assert len({*briefs.values()}) == 3, "the three calls sent the same instruction"


def test_an_unknown_group_raises_rather_than_prompting_without_a_brief():
    with pytest.raises(ValueError, match="unknown roster group"):
        news.system_instruction("taxi-squad", 2026, 2, TODAY)


def test_the_rules_forbid_answering_from_recall():
    """Rule 1 is the load-bearing one. A model that falls back on training
    data produces confident, plausible, stale news that nothing downstream
    can distinguish from a real finding."""
    rules = news.NEWS_HOUSE_RULES

    assert "Never" in rules and "training" in rules
    assert "start/sit" in rules, "nothing stops the news contradicting the summary"
    assert "One search per player" in rules, "nothing asks the model to bound the billed meter"


def test_the_news_rules_are_not_the_summary_rules():
    """The two prompts have opposite contracts -- one forbids outside
    knowledge, the other exists to fetch it. Sharing text between them is
    how that distinction quietly dies."""
    from espn_ff.ai import prompt

    assert news.NEWS_HOUSE_RULES != prompt.HOUSE_RULES
    assert "do not supply outside knowledge" not in news.NEWS_HOUSE_RULES


def test_the_user_message_carries_the_id_name_team_and_slot_of_every_player():
    groups = news.roster_groups(FULL_ROSTER, week=2, team_id=5)
    message = news.user_message(groups["starters"], 2026, 2, TODAY)

    for token in ("11", "Starter One", "CHI", "QB", "22", "Starter Two"):
        assert token in message
    assert "2 players" in message


def test_the_user_message_labels_espn_injury_status_as_context_not_news():
    """Handed unqualified, it invites the model to echo it back as a finding
    -- rule 6 -- and it can be days old."""
    groups = news.roster_groups(FULL_ROSTER, week=2, team_id=5)
    message = news.user_message(groups["ir"], 2026, 2, TODAY)

    assert "days old" in message and "context" in message


def test_an_empty_group_is_never_prompted():
    """A billed grounded call to be told nobody is on IR is waste. The
    caller skips the group; this is the backstop for a caller that forgets."""
    with pytest.raises(ValueError, match="empty group is skipped"):
        news.user_message(FULL_ROSTER.iloc[0:0], 2026, 2, TODAY)


# --- reading the answer back ---------------------------------------------


def group_df():
    return news.roster_groups(FULL_ROSTER, week=2, team_id=5)["starters"]


def answer(players):
    return json.dumps({"players": players})


def test_a_clean_answer_is_reconciled_onto_the_roster_rows():
    players, warnings = news.parse_players(
        answer(
            [
                {"player_id": 11, "found": True, "headline": "Limited in practice.",
                 "detail": "Wednesday report.", "as_of": "2026-09-16"},
                {"player_id": 22, "found": True, "headline": "Full participant.",
                 "detail": "No designation.", "as_of": "2026-09-15"},
            ]
        ),
        group_df(),
        "starters",
    )

    assert warnings == []
    assert [p["player_id"] for p in players] == [11, 22]
    assert players[0]["headline"] == "Limited in practice."
    assert players[0]["group"] == "starters"


def test_identity_comes_from_the_roster_not_from_the_model():
    """The model supplies `player_id` and the news. If it could also supply
    the name, a wrong id would silently rename one player into another."""
    players, _ = news.parse_players(
        answer([{"player_id": 11, "found": True, "headline": "x", "detail": "y",
                 "as_of": "2026-09-16", "player_name": "Somebody Else",
                 "pro_team": "GB"}]),
        group_df(),
        "starters",
    )

    assert players[0]["player_name"] == "Starter One"
    assert players[0]["pro_team"] == "CHI"


def test_a_player_the_model_skipped_is_materialised_as_not_found():
    """Absence and "no news" are different findings. If a skipped player
    simply vanished from the array, a reader could not tell which they were
    looking at -- and that is the whole reason this field is structured."""
    players, warnings = news.parse_players(
        answer([{"player_id": 11, "found": True, "headline": "x", "detail": "y",
                 "as_of": "2026-09-16"}]),
        group_df(),
        "starters",
    )

    assert len(players) == 2, "the roster and the output disagree on how many players exist"
    missing = next(p for p in players if p["player_id"] == 22)
    assert missing["found"] is False
    assert missing["headline"] is None
    assert "no entry" in missing["note"]
    assert any("Starter Two" in w for w in warnings)


def test_a_player_who_is_not_on_the_roster_is_dropped_and_named():
    players, warnings = news.parse_players(
        answer(
            [
                {"player_id": 11, "found": True, "headline": "x", "detail": "y",
                 "as_of": "2026-09-16"},
                {"player_id": 999, "found": True, "headline": "Invented.",
                 "detail": "z", "as_of": "2026-09-16"},
            ]
        ),
        group_df(),
        "starters",
    )

    assert {p["player_id"] for p in players} == {11, 22}
    assert any("999" in w for w in warnings)


def test_found_false_clears_the_prose_rather_than_storing_empty_strings():
    players, _ = news.parse_players(
        answer([{"player_id": 11, "found": False, "headline": "", "detail": "", "as_of": ""},
                {"player_id": 22, "found": True, "headline": "x", "detail": "y", "as_of": ""}]),
        group_df(),
        "starters",
    )

    blank = next(p for p in players if p["player_id"] == 11)
    assert blank["found"] is False
    assert (blank["headline"], blank["detail"], blank["as_of"]) == (None, None, None)
    # as_of is allowed to be unknown on a real finding -- rule 2 prefers that
    # to a default of today's date.
    found = next(p for p in players if p["player_id"] == 22)
    assert found["found"] is True and found["as_of"] is None


def test_found_true_with_no_headline_is_treated_as_not_found():
    """`found` is the model's own claim. An empty headline beside it is a
    contradiction, and resolving it towards "nothing here" is the direction
    that cannot invent a finding."""
    players, _ = news.parse_players(
        answer([{"player_id": 11, "found": True, "headline": "  ", "detail": "y",
                 "as_of": "2026-09-16"}]),
        group_df(),
        "starters",
    )

    assert next(p for p in players if p["player_id"] == 11)["found"] is False


def test_a_fenced_block_is_still_read():
    """The response schema should make fences impossible, but it is a
    Preview feature on one model series -- so a formatting regression must
    not cost a whole report."""
    payload = answer([{"player_id": 11, "found": True, "headline": "x", "detail": "y",
                       "as_of": "2026-09-16"}])
    players, _ = news.parse_players(f"```json\n{payload}\n```", group_df(), "starters")

    assert players[0]["headline"] == "x"


@pytest.mark.parametrize(
    "text", ["not json at all", "[]", '{"summary": "wrong shape"}', ""],
    ids=["prose", "bare-array", "wrong-key", "empty"],
)
def test_an_unreadable_answer_raises_rather_than_storing_a_partial_block(text):
    """Raising leaves news null, which the next run sees and retries. A
    half-populated block would look complete forever."""
    with pytest.raises(news.NewsFormatError):
        news.parse_players(text, group_df(), "starters")


# --- the request shape ----------------------------------------------------


def test_the_response_schema_requires_an_id_and_a_found_flag_per_player():
    schema = news.response_format()["text"]

    assert schema["mimeType"] == "application/json"
    item = schema["schema"]["properties"]["players"]["items"]
    assert {"player_id", "found"} <= set(item["required"])
    assert item["properties"]["player_id"]["type"] == "integer"


def test_the_module_reads_no_clock_and_no_disk():
    """Purity is what lets every test above run with no fixtures, no key and
    no network. `today` is an argument for exactly this reason."""
    source = (news.__file__)
    text = open(source).read()

    for forbidden in ("datetime.now", "date.today", "open(", "read_text", "requests"):
        assert forbidden not in text, f"ai/news.py reached for {forbidden}"


def test_a_missing_injury_status_renders_as_the_repo_null_cell():
    """A D/ST has no injury status. Rendering the literal "None" invites the
    model to read it as a designation; `--` is what every rendered report in
    this repo uses for an absent value."""
    defense = roster([player(-16026, "Seahawks D/ST", "D/ST", True,
                             position="D/ST", team="SEA", status=None)])
    message = news.user_message(defense, 2026, 2, TODAY)

    assert "None" not in message
    assert message.rstrip().splitlines()[-3].endswith("--")
