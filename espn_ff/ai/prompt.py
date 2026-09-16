"""Prompt assembly. Pure -- no disk, no network, no clock.

Everything here is either a literal constant or a function of arguments the
caller already loaded, which is what makes the whole prompt surface
unit-testable with no fixtures and no API key. `summarize.py` owns the
reading.

Most of the system instruction is **material that already exists in this
repo**, passed in verbatim rather than paraphrased here, so it tracks the
docs as they change instead of drifting into a second, staler copy:

  1. League facts        -- from data/out/ exports (teams, roster-slots)
  2. Column dictionary   -- docs/data-sources.md, verbatim
  3. Report semantics    -- docs/report-weekly-schedule.md, verbatim
  4. Derived columns     -- DERIVED_COLUMNS below, the one hand-written part
  5. House rules         -- HOUSE_RULES below
  6. Output contract     -- OUTPUT_CONTRACT below

Section 4 is hand-written because it has to be: these columns appear as
table headers in the rendered reports and are defined nowhere under
`docs/`, only in the docstrings of the modules that compute them. Without
this section the model invents a definition for each one.
"""

# Hard cap stated to the model and pinned by a test, so the two cannot
# drift. `client.MAX_OUTPUT_TOKENS` is set generously against this.
WORD_CAP = 220

PRIOR_COUNT = 4

# --- section 4: derived columns that exist only in docstrings -------------
#
# Each entry names where the definition actually lives, so the next person
# to change one of those modules can find this copy.

DERIVED_COLUMNS = """\
`gap` (waiver add-candidate blocks, espn_ff/report/waivers.py)
    A candidate free agent's `week_projected` minus `bench_floor`'s
    projection, where `bench_floor` is the weakest non-IR bench player
    eligible for that slot. It is the projected points gained by adding the
    candidate and benching the floor -- not by adding them outright. When a
    slot has no eligible bench player at all (a live case: a bench of 3 WR /
    2 RB / 1 QB leaves TE/K/D-ST with no floor), `gap` renders as
    insufficient data and a `starter_context` column carries the current
    starter's own projection beside it. That context is never a substitute
    for the gap.

`gap` (regret table, espn_ff/report/tuesday.py)
    A different column with the same name, in a retrospective table. For one
    started player, the margin by which the best eligible bench player
    outscored them last week. Each row is an independent counterfactual with
    no assignment constraint, so the same bench player may legitimately
    answer several rows. See HOUSE RULES on why these must never be summed.

`ROS projection` (drop candidates, espn_ff/report/tuesday.py)
    Rest-of-season projection, **Inferred** as `season_projected -
    season_points`. player-pool.csv publishes no rest-of-season column; this
    is this repo's own arithmetic, not an ESPN figure. A bench player with
    no pool row renders insufficient data, never a zero.

`tier` (espn_ff/sleeper/signals.py:availability_tier)
    One of OUT, HIGH_RISK, COIN_FLIP, LIKELY_PLAYS, UNKNOWN, CLEAR, derived
    from an injury status plus a practice participation value. UNKNOWN is a
    deliberate sixth value, not a null: a Questionable player with no
    practice value on record cannot resolve to a practice-driven tier, and
    calling that COIN_FLIP would claim more certainty than the data
    supports. Treat UNKNOWN as "unresolved", never as "in between".

Availability precedence (espn_ff/report/availability.py)
    Three sources are shown side by side and never silently collapsed:
    nflverse `report_status` (the official, week-keyed designation), then
    Sleeper `tier`, then ESPN `injury_status`. That order decides only which
    one populates the `tier` column; all three always render, so a
    disagreement between them is visible rather than resolved behind the
    reader's back. A disagreement is worth mentioning; do not adjudicate it.

Free agents (espn_ff/report/pool.py)
    An anti-join, not a column read: player-pool.csv's `on_team_id` is 0 on
    every row (it comes from an ownership-free default pool) and must never
    be read as ownership. Free agents for week N are the week-N pool rows
    minus every week-N roster row across all teams. "Free agent" therefore
    means unowned *and* inside ESPN's default ~1,041-player pool -- a player
    outside that pool never appears as a free agent even though no team
    rosters them either.

`week_projection` source label (espn_ff/report/waivers.py)
    A projection is labelled `pool` when it came from player-pool.csv and
    `roster` when it fell back to a roster row's `projected`. A block mixing
    the two is flagged in the report. The label is provenance, not quality
    -- do not describe a `roster`-sourced number as less reliable unless the
    report itself says so.

Rendering conventions (espn_ff/report/render.py)
    `--` is a null cell. `_(none)_` is an empty table, meaning the query ran
    and returned nothing -- not that the section failed. The literal string
    `insufficient data` means the input was missing or unusable and the
    report is deliberately declining to print a figure.
"""

# --- section 5: house rules ----------------------------------------------

HOUSE_RULES = """\
1.  Tag any non-obvious claim about cadence, freshness, or behaviour as
    **Observed** (measured from an artifact), **Documented** (asserted by a
    vendor or by a docstring in this repo), or **Inferred** (deduced from
    how the code behaves, not published anywhere). Do not tag the obvious.

2.  Never substitute a number where the report says `insufficient data`.
    That string is a deliberate refusal to print a figure, not a gap for you
    to fill from a prior week, from a neighbouring row, or from your own
    knowledge of the player. Say the report cannot see it.

3.  **Never sum per-slot regret gaps.** Each regret row is an independent
    counterfactual over the same bench, so the same player can answer more
    than one row and the column does not add. `optimal_lineup` is the single
    number in these reports that legitimately aggregates the week.

4.  The waiver report's WR and RB/WR candidate blocks repeat rows by design
    -- a WR is eligible at both slots, so both blocks return the same top
    candidates. That is not duplication to flag. Likewise the `drop`
    pairings beside an add candidate are alternatives, not a set of
    simultaneous moves: adding one player does not require dropping all of
    them.

5.  `trending_add` is display-only. It is a community add count from
    Sleeper, carried for colour; it never enters a sort key, a gap, or a
    recommendation, and neither should your summary.

6.  Freshness comes from a feed's own timestamp -- the Freshness block at
    the top of the report -- never from when a file was written. A STALE
    flag there is a real finding and belongs in the closing sentence.

7.  Retrospective regret is not a start/sit rule. "Player X outscored your
    starter last week" is a description of last week, not an instruction for
    this week. Do not convert a regret row into a lineup recommendation.

8.  Report only what this report says. Do not import a number from a prior
    week's report except to state that something changed, and do not supply
    outside knowledge about a player, a team, or an injury.
"""

# --- section 6: output contract ------------------------------------------

OUTPUT_CONTRACT = f"""\
Write plain markdown prose. Specifically:

-   No heading of your own. The summary is stored on its own and a reader
    already knows which report it belongs to.
-   No tables, no bullet lists of candidates. Prose paragraphs only.
-   At most {WORD_CAP} words. Shorter is better; a summary that needs the
    whole budget usually has not decided what matters.
-   **Lead with the decision due.** The first sentence names the single
    action this report exists to inform -- who to start, who to claim, who
    to watch -- or states plainly that no action is due.
-   Then say what changed since the prior reports you were given. If there
    are no prior reports, say so once and move on; do not speculate about
    what a previous week might have shown.
-   **Close by naming what this report explicitly cannot see** -- a STALE
    feed, an `insufficient data` cell that matters, a section that rendered
    `_(none)_`. If nothing is missing, say that instead. Never end on a
    recommendation with an unstated blind spot behind it.
-   Inline code formatting for column names and literal values is welcome;
    bold is not.
"""


def league_facts(teams_df, roster_slots_df, season, league_id, team_id):
    """Section 1: who we are and what a legal lineup looks like.

    Takes DataFrames rather than reading them, so this stays pure. Either
    may be empty -- a fresh runner whose `restore-out` found nothing yet --
    in which case the corresponding line says so outright rather than being
    omitted, since a missing line is indistinguishable from a league with
    no teams.
    """
    lines = [
        f"League id {league_id}, season {season}. The reader's team is team_id {team_id}.",
        "",
    ]

    if teams_df is None or teams_df.empty:
        lines.append("Team names: unavailable in this run (no teams export on disk).")
    else:
        our = teams_df[teams_df["team_id"] == team_id]
        if not our.empty:
            lines.append(f"The reader's team is \"{our['team_name'].iloc[0]}\".")
        lines.append("All teams in the league:")
        for _, row in teams_df.iterrows():
            lines.append(f"  {row['team_id']:>3}  {row['team_name']}")

    lines.append("")

    if roster_slots_df is None or roster_slots_df.empty:
        lines.append("Starting lineup: unavailable in this run (no roster-slots export on disk).")
    else:
        starters = roster_slots_df[~roster_slots_df["slot"].isin(("BE", "IR"))]
        shape = ", ".join(f"{int(r['count'])}x {r['slot']}" for _, r in starters.iterrows())
        bench = roster_slots_df[roster_slots_df["slot"].isin(("BE", "IR"))]
        bench_shape = ", ".join(f"{int(r['count'])}x {r['slot']}" for _, r in bench.iterrows())
        lines.append(f"Starting lineup: {shape}.")
        if bench_shape:
            lines.append(f"Bench: {bench_shape}.")

    return "\n".join(lines)


def system_instruction(facts, column_dictionary, report_semantics, day):
    """Assemble the six sections into one instruction.

    `day` is a REPORTS key ("monday", "tuesday", "tuesday-waivers",
    "wednesday", "thursday"). It is named rather than used to select a subset of the
    material: the report-semantics document covers every day, and telling
    the model which day it is reading is cheaper and less brittle than
    slicing that document by heading.
    """
    return "\n".join(
        [
            "You summarise one weekly fantasy-football report for the person who "
            "owns the team it is written for. They will read your summary instead "
            "of the report when they are short on time, so it has to be true "
            "before it is useful.",
            "",
            f"The report you are about to read is the `{day}` report.",
            "",
            "=" * 72,
            "SECTION 1 -- LEAGUE FACTS",
            "=" * 72,
            facts,
            "",
            "=" * 72,
            "SECTION 2 -- COLUMN DICTIONARY (docs/data-sources.md, verbatim)",
            "=" * 72,
            "This is the field-level data dictionary for every column the reports",
            "are built from. It carries the traps next to the meanings; read those",
            "as binding.",
            "",
            column_dictionary,
            "",
            "=" * 72,
            "SECTION 3 -- REPORT SEMANTICS (docs/report-weekly-schedule.md, verbatim)",
            "=" * 72,
            "What each report is for and what decision it is due to inform.",
            "",
            report_semantics,
            "",
            "=" * 72,
            "SECTION 4 -- DERIVED COLUMNS",
            "=" * 72,
            "These appear as table headers in the reports and are defined nowhere",
            "in Section 2. Do not infer a definition for any of them.",
            "",
            DERIVED_COLUMNS,
            "",
            "=" * 72,
            "SECTION 5 -- HOUSE RULES",
            "=" * 72,
            HOUSE_RULES,
            "",
            "=" * 72,
            "SECTION 6 -- OUTPUT",
            "=" * 72,
            OUTPUT_CONTRACT,
        ]
    )


def user_message(report_text, report_label, priors):
    """Today's report in full, then up to PRIOR_COUNT prior same-type
    reports oldest-first.

    `priors` is a list of (label, text) pairs. An empty list says so
    outright -- week 1 has no priors, and leaving the model to infer why
    invites it to explain the absence as a data problem.
    """
    parts = []

    if priors:
        parts.append(
            f"Here are the {len(priors)} most recent prior `{report_label}` reports, "
            "oldest first. They are context for what changed -- do not summarise them."
        )
        parts.append("")
        for label, text in priors:
            parts.append("-" * 72)
            parts.append(f"PRIOR REPORT: {label}")
            parts.append("-" * 72)
            parts.append(text)
            parts.append("")
    else:
        parts.append(
            "There are no prior reports of this type. This is the first one -- "
            "for a week-1 report that is expected, not a gap in the data. Say so "
            "once rather than speculating about what an earlier week held."
        )
        parts.append("")

    parts.append("=" * 72)
    parts.append(f"THE REPORT TO SUMMARISE: {report_label}")
    parts.append("=" * 72)
    parts.append(report_text)

    return "\n".join(parts)
