"""Numbers a person can change: where each lives, and what ships."""

from __future__ import annotations

GLOBAL_PARAMS: dict[str, float] = {
    "worker:idle_release_s": 600.0,
    "sample:floor": 8.0,
    "sample:per_root": 2.0,
    "sample:min_file_bytes": 30000.0,
    "sample:min_dimension": 400.0,
    "sample:blank_ratio": 0.95,
    "sample:near_white": 240.0,
    "sample:near_black": 15.0,
    "sample:tail_skip": 0.95,
    "sample:zone_early": 0.25,
    "sample:zone_middle": 0.65,
    "sample:weight_early": 0.20,
    "sample:weight_middle": 0.50,
    "sample:weight_late": 0.30,
    "video:budget_base": 8.0,
    "video:budget_per_minute": 6.0,
    "video:budget_min": 4.0,
    "video:budget_max": 0.0,
    "video:edge_margin": 0.03,
    "video:scene_distance": 12.0,
    "video:frame_max_size": 1280.0,
    "color:saturation": 0.15,
}

PROFILE_PARAMS: dict[str, float] = {
    "score:default": 0.35,
    "freq:default": 0.30,
    "freqz:default": 0.00,
    "score:general": 0.35,
    "freq:general": 0.30,
    "score:character": 0.50,
    "freq:character": 0.25,
    "score:copyright": 0.50,
    "freq:copyright": 0.25,
    "score:artist": 0.50,
    "freq:artist": 0.25,
    "rating:score": 0.0,
    "rating:min_pages": 1.0,
    "check:near": 0.92,
    "color:page_ratio": 0.05,
    "color:full": 0.70,
    "color:gray": 0.20,
    "type:comic": 0.15,
    "type:illust": 1.00,
    "cg:min_images": 10,
    "cg:max_images": 80,
    "cg:seq_ratio": 0.80,
    "cg:aspect_std": 0.08,
    "cg:min_avg_size": 200000,
}


def _values(rows: dict[str, float]) -> str:
    return ",\n    ".join(f"('{k}', {v!r})" for k, v in rows.items())


SEED_SQL = """
INSERT INTO derivation_params (name, value) VALUES
    """ + _values(GLOBAL_PARAMS) + """
ON CONFLICT (name) DO NOTHING;

INSERT INTO model_profiles (name, active, maintained)
SELECT 'default', TRUE, TRUE
WHERE NOT EXISTS (SELECT 1 FROM model_profiles);

INSERT INTO model_profile_params (profile_id, name, value)
SELECT p.id, d.name, d.value
FROM model_profiles p
CROSS JOIN (VALUES
    """ + _values(PROFILE_PARAMS) + """
) AS d(name, value)
ON CONFLICT (profile_id, name) DO NOTHING;
"""


DERIVED_PARAM_PREFIXES = ("score:", "freq:", "freqz:", "rating:", "type:",
                          "cg:", "color:page_ratio", "color:full",
                          "color:gray")


def is_derived_param(name: str) -> bool:
    """Whether a derived row reads this parameter, so changing it needs a
    recompute rather than a rescan."""
    return name.startswith(DERIVED_PARAM_PREFIXES)


IMMEDIATE_PARAMS = ("worker:idle_release_s",)


def effect(name: str) -> str:
    """When a change to this parameter is felt: `now`, at the next scan or
    check (`next_scan`), or once the profile re-derives (`rederive`)."""
    if is_derived_param(name):
        return "rederive"
    return "now" if name in IMMEDIATE_PARAMS else "next_scan"


def is_profile_param(name: str) -> bool:
    """Whether this parameter belongs to a model profile rather than to
    the installation."""
    return is_derived_param(name) or name == "check:near"


async def params(conn, defaults: dict[str, float]) -> dict[str, float]:
    """Parameters by name, from the installation or the active profile,
    defaulting to the caller's."""
    rows = await conn.fetch(
        "SELECT name, value FROM derivation_params WHERE name = ANY($1::text[]) "
        "UNION ALL "
        "SELECT name, value FROM model_profile_params "
        "WHERE profile_id = active_profile_id() AND name = ANY($1::text[])",
        list(defaults))
    stored = {r["name"]: float(r["value"]) for r in rows}
    return {k: stored.get(k, v) for k, v in defaults.items()}


async def set_param(conn, name: str, value: float,
                    profile: int | None = None) -> None:
    """Change a parameter where it lives: the given or active profile for a
    profile parameter, the installation otherwise."""
    if is_profile_param(name):
        await conn.execute(
            "INSERT INTO model_profile_params (profile_id, name, value) "
            "VALUES (COALESCE($1::smallint, active_profile_id()), $2, $3) "
            "ON CONFLICT (profile_id, name) DO UPDATE SET value = EXCLUDED.value",
            profile, name, value)
    else:
        await conn.execute(
            "INSERT INTO derivation_params (name, value) VALUES ($1, $2) "
            "ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value",
            name, value)


def param_default(name: str) -> float | None:
    """What this build ships for a parameter, or None if it ships nothing."""
    return GLOBAL_PARAMS.get(name, PROFILE_PARAMS.get(name))
