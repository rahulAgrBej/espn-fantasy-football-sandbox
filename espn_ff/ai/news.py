"""Grounded roster news: prompt assembly, response schema, and the
reconciliation that makes the result checkable.

Pure -- no disk, no network, no clock. `today` is an argument and rosters
arrive as DataFrames, which is what makes this whole surface unit-testable
with no fixtures and no API key. `summarize.py` owns the reading and the
calling, exactly as it does for `prompt.py`.

**This module is the opposite of `prompt.py`, deliberately.** That one
summarises a report and its HOUSE_RULES #8 forbids outside knowledge: "do
not supply outside knowledge about a player, a team, or an injury." This one
exists to go get precisely that, because every feed the reports are built
from (ESPN, Sleeper, nflverse, The Odds API) lags the real world by hours to
days. The two prompts therefore share nothing -- not a system instruction,
not a rule list, not a tone -- and the news must never be phrased as a
lineup call, or the two halves of one envelope will contradict each other.

Three calls per report, not one and not seventeen:

  starters  the most recent item each -- injury, practice, usage, matchup
  bench     one line each, and only where something actually changed
  ir        designation and return timeline only; skipped when IR is empty

Grouping is what lets each group carry its own depth instruction. It also
bounds the metered part: Google bills **per search query the model chooses
to execute**, not per request, so a per-player fan-out would multiply the
meter by the roster size for no extra signal. See NEWS_HOUSE_RULES rule 7
and docs/ai-summaries.md's "The grounding budget".
"""

import json

# The lineup_slot value ESPN uses for injured reserve. `started == False`
# alone is NOT a bench filter -- it is true for IR (and ER) too, the trap
# already codified in espn_ff/report/tuesday.py:_bench.
IR_SLOT = "IR"

GROUPS = ("starters", "bench", "ir")

# Per-group depth. Kept beside the group list rather than inside
# `system_instruction` so a reader can see all three asks at once and tell
# how they differ -- the difference is the entire reason there are three
# calls instead of one.
GROUP_BRIEFS = {
    "starters": """\
These players are in this week's starting lineup. For each one, report the
single most recent item that a manager would want to know before kickoff --
an injury designation, a practice report, a usage or depth-chart change, or
a matchup note. Prefer the most recent; do not stack three items on one
player while another gets none.""",
    # The "nothing changed is a fine answer" permission below is load-bearing
    # and was also, in its first form, the reason this call skipped searching
    # more often than the others: told that silence was acceptable, the model
    # would conclude nothing had changed without looking. Hence the explicit
    # search-first instruction -- `found: false` has to be the *result* of a
    # search, not a substitute for one.
    "bench": """\
These players are on the bench. Search for each of them first. Then give one
line each, and only where something has actually changed -- an injury, a
role change, a promotion, a snap-share move.

If the search turns up nothing new for a player, that is a legitimate and
useful answer: set `found` to false and say so. But reach it by searching,
never by assuming a bench player is quiet. "Nothing changed" is a finding;
"I did not look" is not, and the two are indistinguishable downstream.""",
    "ir": """\
These players are on injured reserve. Report only their current designation
and return timeline -- whether they have been activated, opened a practice
window, or been ruled out further. Speculation about a return date that no
source has given is not a timeline.""",
}

# --- the house rules, written against how THIS call goes wrong ------------

NEWS_HOUSE_RULES = """\
1.  Every claim must come from a search result you retrieved in this turn.
    If the search returns nothing for a player, set `found` to false and say
    so. **Never** fall back on what you already know about the player from
    training. Recall stated as this week's news is the single worst output
    available here -- it is confidently wrong, it looks exactly like a real
    finding, and nothing downstream can tell the difference.

2.  Date every item in `as_of`, as YYYY-MM-DD. If you cannot establish when
    something happened, leave `as_of` empty rather than defaulting to
    today's date. An undated item presented as today's news is rule 1 in a
    different costume.

3.  **Never turn news into a start/sit call.** The lineup decision belongs
    to the report this news is stored beside, and that report cannot see
    what you found. A recommendation here contradicts the summary sitting
    in the same file. Report what happened; the reader decides.

4.  Give a designation in the source's own words -- Questionable, DNP,
    limited, full participant. Do not translate it into a tier, a
    probability, or a confidence level. This repo computes an availability
    tier elsewhere, from practice data, and you are not computing it.

5.  Check that the player you found is the player you were given. The NFL
    team is in the table for exactly this reason: a namesake on another
    roster is a wrong answer, not a near miss. If you cannot confirm the
    identity, `found` is false.

6.  The roster row is not news. "He is the starting quarterback" and "he
    plays for Chicago" restate the input you were handed.

7.  **One search per player.** Where several players share an NFL team,
    search that team once and read across the result rather than issuing a
    query per name. Each query is separately billed, so a careless fan-out
    costs real money for no extra signal.

8.  Be brief. `headline` is one clause. `detail` is one or two sentences of
    substance -- what happened, when, and per whom. Neither is a paragraph.
"""

# --- the response schema --------------------------------------------------
#
# `found` plus three strings, and no nullable types: the JSON Schema subset
# Gemini accepts is narrower than the full spec, and "" is unambiguous here
# because no real headline is empty. `parse_players` normalises "" back to
# None so the stored envelope carries real nulls rather than empty strings.

PLAYER_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "player_id": {
            "type": "integer",
            "description": "Copy the player_id from the table exactly. Do not invent one.",
        },
        "found": {
            "type": "boolean",
            "description": "True only if a search result in this turn supports the item.",
        },
        "headline": {"type": "string", "description": "One clause. Empty when found is false."},
        "detail": {"type": "string", "description": "One or two sentences. Empty when found is false."},
        "as_of": {"type": "string", "description": "YYYY-MM-DD, or empty when the date is unknown."},
    },
    "required": ["player_id", "found", "headline", "detail", "as_of"],
}

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {"players": {"type": "array", "items": PLAYER_ITEM_SCHEMA}},
    "required": ["players"],
}


# `mimeType` is a protobuf **enum** (TextResponseFormat.MimeType), not the
# MIME string the vendor's own example shows. Sending "application/json"
# there is a 400, Observed 2026-09-16:
#
#   Invalid value at 'generation_config.response_format.text.mime_type'
#   (...v1beta.TextResponseFormat.MimeType), "application/json"
#
# Loudly wrong rather than silently ignored, which is the only reason this
# was cheap to find. `APPLICATION_JSON` is the accepted value.
JSON_MIME_TYPE = "APPLICATION_JSON"


def response_format():
    """The `generationConfig.responseFormat` value for a news call.

    Combining a response schema with a built-in tool is a **Preview**
    feature of the Gemini 3 series (Documented -- Google). It constrains the
    shape; it does not make the content true, and a model outside that
    series loses the guarantee with no error. `parse_players` therefore
    validates rather than assumes, and its failure path stays tested.
    """
    return {"text": {"mimeType": JSON_MIME_TYPE, "schema": RESPONSE_SCHEMA}}


class NewsFormatError(ValueError):
    """The model's response could not be read as the agreed shape.

    A ValueError rather than a GeminiError: nothing went wrong with the
    request, the answer just did not arrive in the contract. `summarize`
    treats it the same way either way -- news becomes null, the summary
    beside it still lands, and the next trigger retries.
    """


# --- roster slicing -------------------------------------------------------


def roster_groups(rosters_df, week, team_id):
    """`{"starters": df, "bench": df, "ir": df}` for one team's week.

    Bench MUST exclude IR explicitly. `started == False` is true for injured
    reserve as well as the bench, so filtering on it alone silently files
    every IR player under "bench" -- and the bench brief asks for what
    changed while the IR brief asks for a return timeline, so the mistake
    would produce plausible, wrongly-framed answers rather than an error.

    An empty or column-less export yields three empty frames rather than
    raising, so a run whose `restore-out` found nothing degrades to an
    explicit "no roster" failure in `summarize` instead of crashing.
    """
    empty = {group: rosters_df.iloc[0:0] for group in GROUPS}
    if rosters_df is None or rosters_df.empty:
        return empty
    if not {"week", "team_id", "started", "lineup_slot"}.issubset(rosters_df.columns):
        return empty

    ours = rosters_df[(rosters_df["week"] == week) & (rosters_df["team_id"] == team_id)]
    on_ir = ours["lineup_slot"] == IR_SLOT
    return {
        "starters": ours[ours["started"] & ~on_ir],
        "bench": ours[~ours["started"] & ~on_ir],
        "ir": ours[on_ir],
    }


# --- prompt assembly ------------------------------------------------------


def system_instruction(group, season, week, today):
    """The instruction for one group's grounded call.

    `group` selects only the brief; the rules, the output contract and the
    date anchor are identical across all three. Anchoring "latest" to an
    explicit date matters more here than in any other prompt in this repo:
    a model with no date given will happily call a months-old item recent.
    """
    if group not in GROUP_BRIEFS:
        raise ValueError(f"unknown roster group {group!r} -- expected one of {GROUPS}")

    return "\n".join(
        [
            "You research current NFL news for the manager of one fantasy football "
            "team. You are given some of their rostered players and you return the "
            "latest real news on each, grounded in Google Search.",
            "",
            f"Today is {today:%Y-%m-%d}. It is season {season}, week {week}. "
            '"Latest" means relative to today, not to whatever you remember.',
            "",
            "This is NOT a summary of a report, and you have not been given one. "
            "Everything you report must come from a search you ran just now.",
            "",
            "=" * 72,
            f"SECTION 1 -- THIS GROUP: {group.upper()}",
            "=" * 72,
            GROUP_BRIEFS[group],
            "",
            "=" * 72,
            "SECTION 2 -- HOUSE RULES",
            "=" * 72,
            NEWS_HOUSE_RULES,
            "",
            "=" * 72,
            "SECTION 3 -- OUTPUT",
            "=" * 72,
            "Return one object per player you were given, in the `players` array, "
            "carrying that player's `player_id` from the table unchanged. Return "
            "every player, including the ones you found nothing for -- an omitted "
            "player and a player with no news must not look the same to the reader.",
            "",
            "Set `found` to false and leave `headline`, `detail` and `as_of` empty "
            "when the search turned up nothing you can stand behind. That is a "
            "real answer and it is preferred over a thin one.",
        ]
    )


def user_message(group_df, season, week, today):
    """The roster table for one group.

    Columns are chosen to make rule 5 checkable: `pro_team` is what lets the
    model reject a namesake, and `injury_status` is ESPN's own value, given
    so the model can confirm or update it rather than rediscover it. It is
    labelled as ESPN's and possibly stale, because presenting it unqualified
    would invite the model to echo it back as news (rule 6).
    """
    if group_df is None or group_df.empty:
        raise ValueError("user_message called with no players -- an empty group is skipped, not prompted")

    lines = [
        f"Season {season}, week {week}. Today is {today:%Y-%m-%d}.",
        "",
        "These are the players. `espn_injury_status` is ESPN's own designation "
        "as of their last export -- it may be days old, it is context rather "
        "than news, and confirming or correcting it is useful while repeating "
        "it is not.",
        "",
        f"{'player_id':>10}  {'player':<26} {'pos':<5} {'team':<5} {'slot':<7} espn_injury_status",
    ]
    for _, row in group_df.iterrows():
        lines.append(
            f"{row['player_id']:>10}  {str(row['player_name']):<26} "
            f"{str(row['position']):<5} {str(row['pro_team']):<5} "
            # `--` rather than a bare None: it is this repo's null cell
            # everywhere else (see prompt.DERIVED_COLUMNS' rendering
            # conventions), and the literal "None" invites the model to read
            # it as a designation. A D/ST carries no injury status at all.
            f"{str(row['lineup_slot']):<7} {_clean(row.get('injury_status')) or '--'}"
        )

    lines.append("")
    lines.append(f"Return one entry for each of the {len(group_df)} players above.")
    return "\n".join(lines)


# --- reading the answer back ----------------------------------------------


def parse_players(text, group_df, group):
    """The model's answer, reconciled against the roster it was given.

    This reconciliation is the entire reason the news is stored as
    structured per-player objects rather than as a prose blob. It
    guarantees, in code rather than by asking the model nicely:

      - **Every rostered player appears.** One the model skipped is
        materialised with `found: false` and a note saying the model
        returned nothing for it. Silence and "no news" are different
        findings and must not render identically.
      - **No player appears who is not on the roster.** An unrecognised
        `player_id` -- a hallucinated one, or a real player copied from a
        search result -- is dropped and named in the returned warnings.
      - **Identity comes from the roster, never from the model.** Name,
        position, team and slot are taken from the DataFrame row. The model
        supplies only `player_id` and the news itself, so it cannot rename
        a player into someone else.

    Returns `(players, warnings)`. Raises NewsFormatError when the response
    is not readable as the agreed shape at all -- the schema is Preview and
    a model outside the Gemini 3 series silently stops honouring it.
    """
    payload = _load(text)
    items = payload.get("players")
    if not isinstance(items, list):
        raise NewsFormatError(
            f"{group}: response carried no `players` array "
            f"(top-level keys: {sorted(payload)[:5]})"
        )

    by_id, warnings = {}, []
    roster_ids = {int(row["player_id"]) for _, row in group_df.iterrows()}
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            player_id = int(item["player_id"])
        except (KeyError, TypeError, ValueError):
            warnings.append(f"{group}: dropped an entry with no usable player_id")
            continue
        if player_id not in roster_ids:
            warnings.append(f"{group}: dropped player_id {player_id}, not on this roster")
            continue
        by_id[player_id] = item

    players = []
    for _, row in group_df.iterrows():
        player_id = int(row["player_id"])
        item = by_id.get(player_id)
        # Identity always from the roster row, never from the model.
        player = {
            "player_id": player_id,
            "player_name": _clean(row.get("player_name")),
            "position": _clean(row.get("position")),
            "pro_team": _clean(row.get("pro_team")),
            "lineup_slot": _clean(row.get("lineup_slot")),
            "group": group,
            "espn_injury_status": _clean(row.get("injury_status")),
        }

        if item is None:
            warnings.append(f"{group}: {player['player_name']} was not in the response")
            player.update(
                found=False, headline=None, detail=None, as_of=None,
                note="the model returned no entry for this player",
            )
            players.append(player)
            continue

        headline = _text(item.get("headline"))
        found = bool(item.get("found")) and headline is not None
        player.update(
            found=found,
            headline=headline if found else None,
            detail=_text(item.get("detail")) if found else None,
            as_of=_text(item.get("as_of")) if found else None,
        )
        if not found:
            player["note"] = "the grounded search returned nothing for this player"
        players.append(player)

    return players, warnings


def _load(text):
    """`json.loads`, tolerating a fenced block.

    The response schema should make fences impossible. It is a Preview
    feature on one model series, so this strips them anyway rather than
    failing a whole report over a formatting regression we were warned
    about.
    """
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    try:
        payload = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError) as exc:
        raise NewsFormatError(f"response was not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise NewsFormatError(f"response was a {type(payload).__name__}, expected an object")
    return payload


def _text(value):
    """A non-empty string, or None. The schema has no nullable type, so ""
    is how the model says "nothing here"; storing it as an empty string
    would make a missing headline look like a present-but-blank one."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _clean(value):
    """A DataFrame cell as a plain string, or None for NaN/missing. Keeps
    numpy scalars and float('nan') out of the JSON envelope."""
    if value is None or value != value:
        return None
    text = str(value).strip()
    return text or None
