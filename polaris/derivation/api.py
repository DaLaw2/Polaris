"""HTTP endpoints for derivation parameters."""

from fastapi import APIRouter
from pydantic import BaseModel

from polaris import state
from polaris.jobs import worker
from polaris.shared import tuning
from polaris.shared.errors import refuse

router = APIRouter()


class ParamRequest(BaseModel):
    """A threshold, and nothing else. The name is the path."""

    value: float


_PARAMS_SQL = """
    SELECT name, value, FALSE AS profile FROM derivation_params
    UNION ALL
    SELECT name, value, TRUE FROM model_profile_params
    WHERE profile_id = $1
"""


def _param_view(r) -> dict:
    value = float(r["value"])
    default = tuning.param_default(r["name"])
    return {"name": r["name"], "value": value, "profile": r["profile"],
            "default": default,
            "adjusted": default is None or value != float(default),
            "group": r["name"].split(":")[0],
            "effect": tuning.effect(r["name"])}


async def _profile(conn, profile: int | None) -> tuple[int, bool]:
    """The profile asked for, or the active one, and whether it is kept
    derived."""
    row = await conn.fetchrow(
        "SELECT id, maintained FROM model_profiles "
        "WHERE id = COALESCE($1::smallint, active_profile_id())", profile)
    if row is None:
        raise refuse(404, "profile_not_found", "no such model profile",
                     profile=profile)
    return row["id"], row["maintained"]


@router.get("/api/params")
async def list_params(profile: int | None = None):
    """Every tunable number: the installation's, and one model profile's,
    the active one unless `profile` names another. `adjusted` compares with
    what this build ships; `effect` says when a change is felt."""
    async with state.store.acquire() as conn:
        pid, _ = await _profile(conn, profile)
        rows = await conn.fetch(
            f"SELECT * FROM ({_PARAMS_SQL}) p ORDER BY name", pid)
    return {"profile": pid, "params": [_param_view(r) for r in rows]}


@router.put("/api/params/{name:path}")
async def set_param(name: str, req: ParamRequest, profile: int | None = None):
    """Move a parameter where it lives, and queue the rebuild of what reads it."""
    async with state.store.acquire() as conn:
        async with conn.transaction():
            pid, maintained = await _profile(conn, profile)
            known = await conn.fetchval(
                f"SELECT 1 FROM ({_PARAMS_SQL}) p WHERE name = $2", pid, name)
            if not known:
                raise refuse(404, "param_not_found",
                             f"no parameter named {name!r}; GET /api/params "
                             f"lists them", name=name)
            await tuning.set_param(conn, name, req.value, pid)
            row = await conn.fetchrow(
                f"SELECT * FROM ({_PARAMS_SQL}) p WHERE name = $2", pid, name)
            deriving = (await worker.enqueue_derive(conn, pid)
                        if tuning.is_derived_param(name) and maintained
                        else None)
    return {**_param_view(row), "profile_id": pid,
            "refresh": "queued" if deriving else "none",
            "deriving": deriving}
