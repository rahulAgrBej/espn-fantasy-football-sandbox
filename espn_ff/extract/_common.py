"""Helpers shared across extractors."""


def team_name(team):
    """2023+ leagues use a single `name`; older ones split location/nickname."""
    name = (team.get("name") or "").strip()
    if not name:
        name = f"{team.get('location', '')} {team.get('nickname', '')}".strip()
    return name or f"Team {team.get('id')}"


def owner_names(team, members_by_id):
    """Resolve a team's owner GUIDs against the league's members[] list."""
    guids = team.get("owners") or []
    names = []
    for guid in guids:
        member = members_by_id.get(guid, {})
        full = f"{member.get('firstName', '')} {member.get('lastName', '')}".strip()
        names.append(full or member.get("displayName") or guid)
    return ", ".join(names)


def members_index(payload):
    return {m.get("id"): m for m in (payload.get("members") or [])}


def teams_index(payload):
    """Teams keyed by id -- never by array position."""
    return {t.get("id"): t for t in (payload.get("teams") or [])}
