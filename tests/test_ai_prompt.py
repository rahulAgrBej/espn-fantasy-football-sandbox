"""Prompt assembly.

`prompt.py` is pure, so all of it is testable with no fixtures, no API key
and no network -- which is the point of keeping the disk reads in
`summarize.py`. What these tests pin is mostly *presence*: the derived
columns and house rules exist because the model invents each of them
otherwise, so a section silently dropping out of the instruction is a
correctness bug that nothing else would catch.
"""

import pandas as pd
import pytest

from espn_ff.ai import prompt

TEAMS = pd.DataFrame(
    {
        "team_id": [1, 5, 9],
        "team_name": ["Team One", "The Reader's Team", "Team Nine"],
    }
)

ROSTER_SLOTS = pd.DataFrame(
    {
        "slot_id": [0, 2, 4, 6, 16, 17, 20, 21],
        "slot": ["QB", "RB", "WR", "TE", "D/ST", "K", "BE", "IR"],
        "count": [1, 2, 2, 1, 1, 1, 6, 1],
    }
)


# --- section 1: league facts ---------------------------------------------


def test_league_facts_name_the_reader_and_the_lineup_shape():
    facts = prompt.league_facts(TEAMS, ROSTER_SLOTS, season=2026, league_id=1681721675, team_id=5)

    assert "The Reader's Team" in facts
    assert "team_id 5" in facts
    assert "1681721675" in facts
    assert "1x QB" in facts and "2x RB" in facts and "2x WR" in facts


def test_league_facts_separate_the_bench_from_the_starting_slots():
    """A summary that treats BE or IR as a startable slot recommends an
    illegal lineup, and nothing downstream would catch it."""
    facts = prompt.league_facts(TEAMS, ROSTER_SLOTS, season=2026, league_id=1, team_id=5)

    starting_line = next(line for line in facts.splitlines() if line.startswith("Starting lineup:"))
    assert "BE" not in starting_line and "IR" not in starting_line
    assert "Bench: 6x BE, 1x IR." in facts


@pytest.mark.parametrize("missing", ["teams", "slots", "both"])
def test_a_missing_export_says_so_outright_rather_than_being_omitted(missing):
    """Same reasoning as render.header_lines' INSUFFICIENT_DATA week line: an
    omitted line is indistinguishable from a league that has no teams."""
    teams = pd.DataFrame() if missing in ("teams", "both") else TEAMS
    slots = pd.DataFrame() if missing in ("slots", "both") else ROSTER_SLOTS

    facts = prompt.league_facts(teams, slots, season=2026, league_id=1, team_id=5)

    if missing in ("teams", "both"):
        assert "Team names: unavailable in this run" in facts
    if missing in ("slots", "both"):
        assert "Starting lineup: unavailable in this run" in facts


def test_league_facts_survive_a_team_id_absent_from_the_export():
    facts = prompt.league_facts(TEAMS, ROSTER_SLOTS, season=2026, league_id=1, team_id=99)

    assert "team_id 99" in facts
    assert "Team One" in facts


# --- sections 4 and 5: the hand-written material -------------------------


@pytest.mark.parametrize(
    "column",
    ["`gap`", "ROS projection", "`tier`", "Availability precedence", "Free agents",
     "`week_projection` source label", "Rendering conventions"],
)
def test_every_derived_column_is_defined(column):
    """These appear as table headers in the rendered reports and are defined
    nowhere under docs/, only in the docstrings of the modules that compute
    them. Anything dropped from here, the model invents."""
    assert column in prompt.DERIVED_COLUMNS


def test_the_two_different_gap_columns_are_told_apart():
    """`gap` means one thing in the waiver add-candidate blocks and another
    in the retrospective regret table. One definition for both is wrong
    twice."""
    assert "espn_ff/report/waivers.py" in prompt.DERIVED_COLUMNS
    assert "espn_ff/report/tuesday.py" in prompt.DERIVED_COLUMNS
    assert prompt.DERIVED_COLUMNS.count("`gap`") >= 2


def test_the_derived_columns_carry_the_on_team_id_trap():
    """`on_team_id` is 0 on every pool row, so reading it as ownership makes
    every player look like a free agent. The definition has to carry the
    trap, not just the anti-join."""
    flattened = " ".join(prompt.DERIVED_COLUMNS.split())

    assert "must never be read as ownership" in flattened
    assert "anti-join" in flattened


@pytest.mark.parametrize(
    "rule",
    [
        "Observed",
        "Documented",
        "Inferred",
        "insufficient data",
        "Never sum per-slot regret gaps",
        "trending_add",
        "never from when a file was written",
        "not a start/sit rule",
    ],
)
def test_every_house_rule_survives(rule):
    assert rule in prompt.HOUSE_RULES


def test_the_output_contract_states_the_word_cap_it_is_checked_against():
    """The cap is a number in two places -- the instruction and
    client.MAX_OUTPUT_TOKENS' headroom. Interpolating it means they cannot
    drift into disagreeing."""
    assert f"At most {prompt.WORD_CAP} words" in prompt.OUTPUT_CONTRACT
    assert prompt.WORD_CAP == 220


def test_the_output_contract_demands_the_decision_first_and_the_blind_spot_last():
    assert "Lead with the decision due" in prompt.OUTPUT_CONTRACT
    assert "cannot see" in prompt.OUTPUT_CONTRACT


def test_the_output_contract_forbids_tables_and_a_heading():
    """A summary is stored on its own and rendered beside its report; a
    heading duplicates context the reader already has, and a table
    reproduces the report rather than summarising it."""
    assert "No heading of your own" in prompt.OUTPUT_CONTRACT
    assert "No tables" in prompt.OUTPUT_CONTRACT


# --- section assembly -----------------------------------------------------


def test_the_system_instruction_carries_all_six_sections():
    instruction = prompt.system_instruction(
        facts="FACTS", column_dictionary="DICT", report_semantics="SEMANTICS", day="tuesday-waivers"
    )

    for heading in (
        "SECTION 1 -- LEAGUE FACTS",
        "SECTION 2 -- COLUMN DICTIONARY",
        "SECTION 3 -- REPORT SEMANTICS",
        "SECTION 4 -- DERIVED COLUMNS",
        "SECTION 5 -- HOUSE RULES",
        "SECTION 6 -- OUTPUT",
    ):
        assert heading in instruction

    assert "FACTS" in instruction and "DICT" in instruction and "SEMANTICS" in instruction
    assert "`tuesday-waivers` report" in instruction


def test_the_docs_go_in_verbatim_not_paraphrased():
    """Passing them through is what makes the prompt track the docs as they
    change instead of drifting into a second, staler copy."""
    dictionary = "| column | Meaning |\n|---|---|\n| points_final | 0.0 while the week is open |"
    instruction = prompt.system_instruction(
        facts="f", column_dictionary=dictionary, report_semantics="s", day="monday"
    )

    assert dictionary in instruction


# --- the user message -----------------------------------------------------


def test_priors_come_before_the_report_oldest_first():
    message = prompt.user_message(
        report_text="THIS WEEK",
        report_label="2026 week 3, rendered 2026-09-22",
        priors=[("week 1", "OLDEST"), ("week 2", "NEWER")],
    )

    assert message.index("OLDEST") < message.index("NEWER") < message.index("THIS WEEK")
    assert "the 2 most recent prior" in message


def test_the_report_to_summarise_is_labelled_unambiguously():
    """Five reports arrive in one message; which one to summarise cannot be
    left to position alone."""
    message = prompt.user_message("TEXT", "2026 week 3, rendered 2026-09-22", [("w1", "a")])

    assert "THE REPORT TO SUMMARISE: 2026 week 3, rendered 2026-09-22" in message


def test_week_one_is_told_it_has_no_priors_rather_than_left_to_infer_it():
    """Silence here reads to the model as a data problem, and it explains
    the absence in the summary. Saying so once costs a sentence."""
    message = prompt.user_message("TEXT", "2026 week 1, rendered 2026-09-08", priors=[])

    assert "There are no prior reports of this type" in message
    assert "TEXT" in message
