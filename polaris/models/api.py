"""HTTP endpoints for models and model profiles."""

import hashlib
import json
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from polaris import state
from polaris.jobs import worker
from polaris.models import definition, profiles
from polaris.shared import tuning
from polaris.shared.errors import refuse

router = APIRouter()

SOLE_ROLES = ("rating", "embedding")


class ProfileRequest(BaseModel):
    """A new profile: empty, or everything `copy_from` holds."""

    name: str
    copy_from: str | None = None


class ProfileEdit(BaseModel):
    """What can change about a profile. Anything unnamed is left alone."""

    name: str | None = None
    maintained: bool | None = None
    active: bool | None = None


class RoleRequest(BaseModel):
    """One category a member answers for, and how its scores are cut."""

    role: str
    score_cutoff: float | None = None
    yields: bool = False


class MemberRequest(BaseModel):
    """A model's standing in a profile: its order within its own series,
    and every category it answers for."""

    rank: int = 0
    roles: list[RoleRequest] = []
    version_id: int | None = None


class DefinitionUpload(BaseModel):
    """A model definition, as TOML text."""

    text: str


class BuildRequest(BaseModel):
    """The precision to build a version at."""

    precision: Literal["fp32", "fp16"] = "fp32"


_PROFILES_SQL = """
    SELECT p.id, p.name, p.active, p.maintained,
           (SELECT COUNT(*) FROM derived.work_search d
            WHERE d.profile_id = p.id) AS works,
           (SELECT j.id FROM scan_jobs j
            WHERE j.kind = 'derive' AND j.model_profile_id = p.id
              AND j.state IN ('queued', 'running')
            ORDER BY j.id LIMIT 1) AS deriving,
           COALESCE(json_agg(json_build_object(
               'id', v.id, 'backend', v.backend,
               'revision', v.revision, 'precision', v.precision,
               'state', v.state, 'rank', m.rank,
               'roles', COALESCE((
                   SELECT json_agg(json_build_object(
                       'role', r.role, 'score_cutoff', r.score_cutoff,
                       'yields', r.yields) ORDER BY r.role)
                   FROM model_profile_roles r
                   WHERE r.profile_id = m.profile_id
                     AND r.model_version_id = m.model_version_id), '[]'),
               'thresholds', COALESCE((
                   SELECT json_object_agg(t.category, t.n)
                   FROM (SELECT category, COUNT(*) AS n FROM derived.tag_thresholds
                         WHERE model_version_id = v.id GROUP BY category) t),
                   '{}'))
               ORDER BY v.backend, m.rank)
               FILTER (WHERE v.id IS NOT NULL), '[]') AS members
    FROM model_profiles p
    LEFT JOIN model_profile_members m ON m.profile_id = p.id
    LEFT JOIN model_versions v ON v.id = m.model_version_id
    GROUP BY p.id
"""


async def _member_version(conn, profile_id: int, backend: str,
                          version_id: int | None) -> int:
    """The version to enrol a backend as: the one named, else the one this
    profile already holds, else the newest ready one."""
    if version_id is not None:
        row = await conn.fetchrow(
            "SELECT backend, state FROM model_versions WHERE id = $1",
            version_id)
        if row is None or row["backend"] != backend:
            raise refuse(404, "version_not_found",
                         f"{backend} has no version {version_id}",
                         id=version_id)
        if row["state"] != "ready":
            raise refuse(409, "version_not_ready",
                         f"{backend} version {version_id} is {row['state']}",
                         id=version_id, state=row["state"])
        return version_id
    vid = await conn.fetchval(
        "SELECT COALESCE("
        "  (SELECT m.model_version_id FROM model_profile_members m"
        "   JOIN model_versions v ON v.id = m.model_version_id"
        "   WHERE m.profile_id = $1 AND v.backend = $2"
        "   ORDER BY m.rank LIMIT 1),"
        "  (SELECT id FROM model_versions"
        "   WHERE backend = $2 AND state = 'ready'"
        "   ORDER BY id DESC LIMIT 1))", profile_id, backend)
    if vid is not None:
        return vid
    if not await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM model_definitions WHERE name = $1)"
            " OR EXISTS (SELECT 1 FROM model_versions WHERE backend = $1)",
            backend):
        raise refuse(404, "model_not_found",
                     f"no model named {backend!r}; GET /api/models lists them",
                     backend=backend)
    raise refuse(409, "no_ready_version",
                 f"{backend} has no ready version; build one first",
                 backend=backend)


@router.get("/api/models")
async def list_models():
    """Every model a definition names: its note and default roles from its
    newest definition (retracted ones last), and every version of it."""
    async with state.store.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT ON (d.name) d.name, d.id AS definition_id,
                   d.retracted_at IS NOT NULL AS retracted,
                   d.spec->>'note' AS note,
                   d.spec->'defaults'->'roles' AS categories,
                   COALESCE((SELECT json_agg(json_build_object(
                                 'id', v.id, 'revision', v.revision,
                                 'precision', v.precision, 'state', v.state,
                                 'definition_id', v.definition_id)
                                 ORDER BY v.id)
                             FROM model_versions v WHERE v.backend = d.name),
                            '[]') AS versions
            FROM model_definitions d
            ORDER BY d.name, d.retracted_at IS NOT NULL, d.id DESC
            """)
    return {"models": [{**dict(r), "categories": json.loads(r["categories"]),
                        "versions": json.loads(r["versions"])} for r in rows]}


@router.get("/api/model-definitions")
async def list_model_definitions():
    """Every stored definition, oldest first per name, with its versions."""
    async with state.store.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT d.id, d.name, d.spec->>'note' AS note,
                   d.spec->'source' AS source, d.source_text, d.checksum,
                   d.uploaded_at, d.retracted_at,
                   COALESCE(json_agg(json_build_object(
                       'id', v.id, 'revision', v.revision,
                       'precision', v.precision, 'state', v.state,
                       'embedding_dim', v.embedding_dim, 'drift', v.drift,
                       'error', v.error, 'created_at', v.created_at)
                       ORDER BY v.id) FILTER (WHERE v.id IS NOT NULL), '[]')
                       AS versions
            FROM model_definitions d
            LEFT JOIN model_versions v ON v.definition_id = d.id
            GROUP BY d.id ORDER BY d.name, d.id
            """)
    return {"definitions": [
        {**dict(r), "source": json.loads(r["source"]),
         "versions": json.loads(r["versions"])} for r in rows]}


@router.post("/api/model-definitions")
async def upload_model_definition(req: DefinitionUpload):
    """Store a definition once it parses. The same text again is the row
    already stored, brought back if it was retracted."""
    try:
        d = definition.parse(req.text)
    except definition.DefinitionError as e:
        raise refuse(422, "definition_invalid", str(e)) from None
    checksum = hashlib.sha256(req.text.encode("utf-8")).hexdigest()
    async with state.store.acquire() as conn:
        async with conn.transaction():
            did = await conn.fetchval(
                "UPDATE model_definitions SET retracted_at = NULL "
                "WHERE checksum = $1 RETURNING id", checksum)
            created = did is None
            if created:
                did = await conn.fetchval(
                    "INSERT INTO model_definitions "
                    "(name, source_text, spec, checksum) "
                    "VALUES ($1, $2, $3, $4) RETURNING id",
                    d.name, req.text, json.dumps(definition.as_json(d)),
                    checksum)
    return {"id": did, "name": d.name, "created": created}


async def _definition_row(conn, definition_id: int):
    row = await conn.fetchrow(
        "SELECT name, source_text, retracted_at FROM model_definitions "
        "WHERE id = $1 FOR UPDATE", definition_id)
    if row is None:
        raise refuse(404, "definition_not_found", "no such model definition",
                     id=definition_id)
    return row


@router.delete("/api/model-definitions/{definition_id}")
async def delete_model_definition(definition_id: int):
    """Delete a definition nothing was built from; one with versions is
    only marked retracted, and its versions keep working."""
    async with state.store.acquire() as conn:
        async with conn.transaction():
            row = await _definition_row(conn, definition_id)
            retract = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM model_versions "
                "WHERE definition_id = $1)", definition_id)
            if retract:
                await conn.execute(
                    "UPDATE model_definitions "
                    "SET retracted_at = COALESCE(retracted_at, NOW()) "
                    "WHERE id = $1", definition_id)
            else:
                await conn.execute(
                    "DELETE FROM model_definitions WHERE id = $1",
                    definition_id)
    return {"id": definition_id, "name": row["name"], "retracted": retract,
            "removed": not retract}


@router.post("/api/model-definitions/{definition_id}/build")
async def build_model_version(definition_id: int, req: BuildRequest):
    """Make a version of a definition at a precision, and queue the job
    that builds it. A failed version of the same revision and precision is
    built again; a ready one, or one a job is building, is refused."""
    async with state.store.acquire() as conn:
        async with conn.transaction():
            row = await _definition_row(conn, definition_id)
            if row["retracted_at"] is not None:
                raise refuse(409, "definition_retracted",
                             f"{row['name']} definition {definition_id} is "
                             f"retracted", id=definition_id)
            d = definition.parse(row["source_text"])
            if d.source.path is None and d.source.revision is None:
                raise refuse(409, "definition_unpinned",
                             f"{d.name} names no source.revision; pin one or "
                             f"use a local path", id=definition_id)
            old = await conn.fetchrow(
                "SELECT v.id, v.state, EXISTS ("
                "  SELECT 1 FROM scan_jobs j WHERE j.model_version_id = v.id"
                "  AND j.state IN ('queued', 'running')) AS live "
                "FROM model_versions v WHERE v.backend = $1 "
                "AND v.revision IS NOT DISTINCT FROM $2 AND v.precision = $3 "
                "FOR UPDATE", d.name, d.source.revision, req.precision)
            if old is not None and (old["state"] == "ready" or old["live"]):
                now = "ready" if old["state"] == "ready" else "building"
                raise refuse(409, "version_exists",
                             f"{d.name} {req.precision} at this revision is "
                             f"{now}", id=old["id"], state=now)
            if old is not None:
                vid = old["id"]
                await conn.execute(
                    "UPDATE model_versions SET definition_id = $2, "
                    "state = 'building', error = NULL, drift = NULL, "
                    "embedding_dim = NULL WHERE id = $1", vid, definition_id)
            else:
                vid = await conn.fetchval(
                    "INSERT INTO model_versions (backend, revision, precision,"
                    " definition_id, state) VALUES ($1, $2, $3, $4, 'building')"
                    " RETURNING id",
                    d.name, d.source.revision, req.precision, definition_id)
            job = await worker.enqueue(conn, "build",
                                       params={"precision": req.precision},
                                       model_version_id=vid)
    return {"definition_id": definition_id, "model_version_id": vid,
            "precision": req.precision, "job_id": job}


_PRUNE_SQL = """
WITH stale AS (
    SELECT DISTINCT s.work_id, s.run_id FROM work_samples s
    WHERE s.work_id = ANY($1::int[])
      AND s.run_id < (SELECT max(x.run_id) FROM work_samples x
                      WHERE x.work_id = s.work_id)
      AND NOT EXISTS (SELECT 1 FROM work_samples x
                      JOIN sample_scores ss ON ss.sample_id = x.id
                      WHERE x.work_id = s.work_id AND x.run_id = s.run_id)
      AND NOT EXISTS (SELECT 1 FROM work_embeddings e
                      WHERE e.work_id = s.work_id AND e.run_id = s.run_id)
), m AS (
    DELETE FROM measurements m USING stale
    WHERE m.work_id = stale.work_id AND m.run_id = stale.run_id)
DELETE FROM work_samples s USING stale
WHERE s.work_id = stale.work_id AND s.run_id = stale.run_id
"""


@router.delete("/api/model-versions/{version_id}")
async def delete_model_version(version_id: int):
    """Forget a version and everything it scored. Refused while a profile
    holds it or a build of it is unfinished. An older run of a work that
    nothing cites any more goes too; a work's newest run stays."""
    async with state.store.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT backend FROM model_versions WHERE id = $1 FOR UPDATE",
                version_id)
            if row is None:
                raise refuse(404, "version_not_found", "no such model version",
                             id=version_id)
            held = [r["name"] for r in await conn.fetch(
                "SELECT p.name FROM model_profile_members m "
                "JOIN model_profiles p ON p.id = m.profile_id "
                "WHERE m.model_version_id = $1 ORDER BY p.name", version_id)]
            if held:
                raise refuse(409, "version_in_use",
                             f"{', '.join(held)} still hold this version",
                             profiles=held)
            if await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM scan_jobs "
                    "WHERE model_version_id = $1 "
                    "AND state IN ('queued', 'running'))", version_id):
                raise refuse(409, "version_building",
                             "a build of this version is unfinished")
            works = [r["work_id"] for r in await conn.fetch(
                "SELECT s.work_id FROM sample_scores ss "
                "JOIN work_samples s ON s.id = ss.sample_id "
                "WHERE ss.model_version_id = $1 "
                "UNION SELECT work_id FROM work_embeddings "
                "WHERE model_version_id = $1", version_id)]
            scores = int((await conn.execute(
                "DELETE FROM sample_scores WHERE model_version_id = $1",
                version_id)).split()[-1])
            for table in ("work_embeddings", "derived.work_vectors"):
                await conn.execute(
                    f"DELETE FROM {table} WHERE model_version_id = $1",
                    version_id)
            await conn.execute(
                "UPDATE check_items SET model_version_id = NULL "
                "WHERE model_version_id = $1", version_id)
            await conn.execute(
                "DELETE FROM scan_jobs WHERE model_version_id = $1", version_id)
            await conn.execute(_PRUNE_SQL, works)
            await conn.execute(
                "DELETE FROM model_versions WHERE id = $1", version_id)
    return {"id": version_id, "backend": row["backend"], "removed": True,
            "scores": scores, "works": len(works)}


@router.get("/api/model-profiles")
async def list_model_profiles():
    """Every model profile: which one is active, its members and their
    roles, how much it has derived, and whether a derive is pending."""
    async with state.store.acquire() as conn:
        rows = await conn.fetch(f"{_PROFILES_SQL} ORDER BY p.id")
        works = await conn.fetchval("SELECT COUNT(*) FROM works")
    return {"works": works,
            "profiles": [{**dict(r), "members": json.loads(r["members"])}
                         for r in rows]}


@router.get("/api/model-profiles/{profile_id}/explain")
async def explain_tag(profile_id: int, tag: str):
    """How one tag is gated for every member and category of a profile:
    the official threshold, the one that applies and where it comes from,
    whether the member yields and to whom, and the coverage gate. Read from
    the same SQL the derivation filters with."""
    async with state.store.acquire() as conn:
        await _profile_row(conn, profile_id)
        rows = await conn.fetch(
            """
            SELECT v.backend, x.model_version_id, x.category,
                   x.official::float8 AS official,
                   r.score_cutoff::float8 AS member_cutoff,
                   (SELECT value FROM model_profile_params
                    WHERE profile_id = $1 AND name = 'score:' || x.category
                   )::float8 AS score_category,
                   (SELECT value FROM model_profile_params
                    WHERE profile_id = $1 AND name = 'score:default'
                   )::float8 AS score_default,
                   x.threshold::float8 AS threshold, x.source, x.yields,
                   NOT x.applies AS skipped,
                   ARRAY(SELECT y.backend FROM model_versions y
                         WHERE y.id = ANY(x.yields_to) ORDER BY y.backend)
                       AS yielded_to,
                   x.freq::float8 AS freq, x.freq_source,
                   x.freqz::float8 AS freqz, x.freqz_source,
                   EXISTS (SELECT 1 FROM model_tags mt
                           WHERE mt.backend = v.backend
                             AND mt.category = x.category
                             AND mt.name = $2) AS emits
            FROM explain_tag($1::smallint, $2) x
            JOIN model_versions v ON v.id = x.model_version_id
            JOIN model_profile_roles r
              ON r.profile_id = $1 AND r.model_version_id = x.model_version_id
             AND r.role = x.category
            ORDER BY v.backend, x.category
            """, profile_id, tag)
    return {"profile_id": profile_id, "tag": tag, "rows": [dict(r) for r in rows]}


@router.get("/api/model-profiles/{profile_id}/coverage")
async def profile_coverage(profile_id: int):
    """Per member, how many works already hold its scores, over the works a
    scan of every searchable collection reaches and over the rest."""
    async with state.store.acquire() as conn:
        await _profile_row(conn, profile_id)
        rows = await conn.fetch(
            """
            WITH live AS (
                SELECT p.work_id, c.searchable
                FROM work_paths p JOIN collections c ON c.name = p.collection
                WHERE p.present
            )
            SELECT v.backend, m.model_version_id,
                   COUNT(*) FILTER (WHERE l.searchable) AS searchable_works,
                   COUNT(*) FILTER (WHERE l.searchable AND x.scored)
                       AS searchable_scored,
                   COUNT(*) FILTER (WHERE NOT l.searchable) AS other_works,
                   COUNT(*) FILTER (WHERE NOT l.searchable AND x.scored)
                       AS other_scored
            FROM model_profile_members m
            JOIN model_versions v ON v.id = m.model_version_id
            CROSS JOIN live l
            CROSS JOIN LATERAL (
                SELECT EXISTS (
                    SELECT 1 FROM work_samples s
                    JOIN sample_scores ss
                      ON ss.sample_id = s.id
                     AND ss.model_version_id = m.model_version_id
                    WHERE s.work_id = l.work_id) AS scored) x
            WHERE m.profile_id = $1
            GROUP BY v.backend, m.model_version_id
            ORDER BY v.backend, m.model_version_id
            """, profile_id)
    return {"profile_id": profile_id, "members": [
        {"backend": r["backend"], "model_version_id": r["model_version_id"],
         "searchable": {"scored": r["searchable_scored"],
                        "works": r["searchable_works"]},
         "other": {"scored": r["other_scored"], "works": r["other_works"]}}
        for r in rows]}


async def _profile_row(conn, profile_id: int):
    row = await conn.fetchrow(
        "SELECT id, name FROM model_profiles WHERE id = $1", profile_id)
    if row is None:
        raise refuse(404, "profile_not_found", "no such model profile",
                     profile=profile_id)
    return row


@router.post("/api/model-profiles")
async def create_model_profile(req: ProfileRequest):
    """A new profile, with this build's parameters or a copy of another's.

    The parameters are written here rather than left to the boot seed: a
    derivation raises on a parameter it cannot read, and the seed only
    runs when the process starts.
    """
    name = req.name.strip()
    if not name:
        raise refuse(400, "name_required", "name the profile")
    async with state.store.acquire() as conn:
        async with conn.transaction():
            src = await _profile_id(conn, req.copy_from) if req.copy_from else None
            pid = await conn.fetchval(
                "INSERT INTO model_profiles (name) VALUES ($1) "
                "ON CONFLICT (name) DO NOTHING RETURNING id", name)
            if pid is None:
                raise refuse(409, "profile_exists",
                             f"a profile named {name!r} exists", name=name)
            if src is None:
                for param, value in tuning.PROFILE_PARAMS.items():
                    await tuning.set_param(conn, param, value, pid)
            else:
                await conn.execute(
                    "INSERT INTO model_profile_params (profile_id, name, value) "
                    "SELECT $1, name, value FROM model_profile_params "
                    "WHERE profile_id = $2", pid, src)
                await conn.execute(
                    "INSERT INTO model_profile_members "
                    "(profile_id, model_version_id, rank) "
                    "SELECT $1, model_version_id, rank "
                    "FROM model_profile_members WHERE profile_id = $2", pid, src)
                await conn.execute(
                    "INSERT INTO model_profile_roles "
                    "(profile_id, model_version_id, role, score_cutoff, yields) "
                    "SELECT $1, model_version_id, role, score_cutoff, yields "
                    "FROM model_profile_roles WHERE profile_id = $2", pid, src)
    return {"id": pid, "name": name, "copied_from": req.copy_from}


@router.patch("/api/model-profiles/{profile_id}")
async def edit_model_profile(profile_id: int, req: ProfileEdit):
    """Rename a profile, maintain it, or make it the one every read sees.

    Activation clears the old row and sets the new one inside one
    transaction: the index that allows a single active profile cannot be
    deferred, so the two states never overlap. A profile that has not
    derived every work is refused -- switching to it would hide the works
    it has not reached yet. Leaving a profile unmaintained drops what it
    derived and cancels its derive jobs, so nothing stale is left to
    switch to; a batch still running cleans up after itself instead of
    holding this request.
    """
    async with state.store.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT id, name, active, maintained FROM model_profiles "
                "WHERE id = $1 FOR NO KEY UPDATE", profile_id)
            if row is None:
                raise refuse(404, "profile_not_found", "no such model profile")
            name = (req.name or row["name"]).strip()
            if name != row["name"] and await conn.fetchval(
                    "SELECT 1 FROM model_profiles WHERE name = $1", name):
                raise refuse(409, "profile_exists",
                             f"a profile named {name!r} exists", name=name)
            active = row["active"] if req.active is None else req.active
            maintained = (row["maintained"] if req.maintained is None
                          else req.maintained)
            if row["active"] and not active:
                raise refuse(409, "profile_active_required",
                             "activate another profile instead; every read "
                             "needs one")
            if active and not maintained:
                raise refuse(409, "profile_active_maintained",
                             "the active profile is always maintained")
            if active and not row["active"]:
                embeddings = await conn.fetchval(
                    "SELECT COUNT(*) FROM model_profile_roles "
                    "WHERE profile_id = $1 AND role = 'embedding'", profile_id)
                if embeddings != 1:
                    raise refuse(
                        409, "profile_needs_embedding",
                        f"{name} has {embeddings} embedding models; an active "
                        f"profile needs exactly one", count=embeddings)
                derived, works = await conn.fetchrow(
                    "SELECT (SELECT COUNT(*) FROM derived.work_search "
                    "        WHERE profile_id = $1), "
                    "       (SELECT COUNT(*) FROM works)", profile_id)
                if derived < works:
                    raise refuse(
                        409, "profile_not_derived",
                        f"{name} has derived {derived:,} of {works:,} "
                        f"works; wait for its derive job",
                        derived=derived, works=works)
                await conn.execute(
                    "UPDATE model_profiles SET active = FALSE WHERE active")
            await conn.execute(
                "UPDATE model_profiles SET name = $2, maintained = $3, "
                "active = $4 WHERE id = $1",
                profile_id, name, maintained, active)
            if row["maintained"] and not maintained:
                running = False
                for job in await conn.fetch(
                        "SELECT id, state FROM scan_jobs WHERE kind = 'derive' "
                        "AND model_profile_id = $1 "
                        "AND state IN ('queued', 'running')", profile_id):
                    await worker.request_cancel(conn, job["id"])
                    running = running or job["state"] == "running"
                if not running:
                    await worker.drop_unmaintained(conn, profile_id)
            queued = (await worker.enqueue_derive(conn, profile_id)
                      if maintained and not row["maintained"] else None)
    return {"id": profile_id, "name": name, "maintained": maintained,
            "active": active, "deriving": queued}


@router.delete("/api/model-profiles/{profile_id}")
async def delete_model_profile(profile_id: int):
    """Forget a profile and everything derived for it.

    Refused while a queued or running job still needs it. Finished jobs
    keep their history and lose only the name of the profile.
    """
    async with state.store.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT name, active FROM model_profiles WHERE id = $1 "
                "FOR UPDATE", profile_id)
            if row is None:
                raise refuse(404, "profile_not_found", "no such model profile")
            if row["active"]:
                raise refuse(409, "profile_is_active",
                             f"{row['name']} is active; activate another first")
            jobs = await conn.fetchval(
                "SELECT COUNT(*) FROM scan_jobs WHERE model_profile_id = $1 "
                "AND state IN ('queued', 'running')", profile_id)
            if jobs:
                raise refuse(409, "profile_has_jobs",
                             f"{jobs:,} unfinished jobs still name "
                             f"{row['name']}", jobs=jobs)
            await conn.execute(
                "UPDATE scan_jobs SET model_profile_id = NULL "
                "WHERE model_profile_id = $1", profile_id)
            await conn.execute(
                "DELETE FROM model_profiles WHERE id = $1", profile_id)
    return {"id": profile_id, "name": row["name"], "removed": True}


@router.put("/api/model-profiles/{profile_id}/members/{backend}")
async def put_model_profile_member(profile_id: int, backend: str,
                                   req: MemberRequest):
    """Put a model in a profile, with the categories it answers for, and
    queue the re-derive that makes the change visible.

    The version is `version_id`, else the one the profile already holds,
    else the newest ready one. Rating and embedding are answered by one
    member each.
    """
    unknown = [r.role for r in req.roles if r.role not in profiles.ROLES]
    if unknown:
        raise refuse(400, "role_unknown",
                     f"not a role: {', '.join(unknown)}; roles are "
                     f"{', '.join(profiles.ROLES)}", roles=unknown)
    async with state.store.acquire() as conn:
        async with conn.transaction():
            maintained = await conn.fetchval(
                "SELECT maintained FROM model_profiles WHERE id = $1 "
                "FOR UPDATE", profile_id)
            if maintained is None:
                raise refuse(404, "profile_not_found", "no such model profile")
            version = await _member_version(conn, profile_id, backend,
                                            req.version_id)
            held = await conn.fetchrow(
                "SELECT r.role, v.backend FROM model_profile_roles r "
                "JOIN model_versions v ON v.id = r.model_version_id "
                "WHERE r.profile_id = $1 AND r.model_version_id <> $2 "
                "AND r.role = ANY($3::text[]) LIMIT 1",
                profile_id, version,
                [r.role for r in req.roles if r.role in SOLE_ROLES])
            if held:
                raise refuse(409, "role_taken",
                             f"{held['backend']} already answers for "
                             f"{held['role']} in this profile",
                             role=held["role"], backend=held["backend"])
            await conn.execute(
                "INSERT INTO model_profile_members "
                "(profile_id, model_version_id, rank) VALUES ($1, $2, $3) "
                "ON CONFLICT (profile_id, model_version_id) "
                "DO UPDATE SET rank = EXCLUDED.rank",
                profile_id, version, req.rank)
            await conn.execute(
                "DELETE FROM model_profile_roles "
                "WHERE profile_id = $1 AND model_version_id = $2",
                profile_id, version)
            for role in req.roles:
                await conn.execute(
                    "INSERT INTO model_profile_roles (profile_id, "
                    "model_version_id, role, score_cutoff, yields) "
                    "VALUES ($1, $2, $3, $4, $5)",
                    profile_id, version, role.role, role.score_cutoff,
                    role.yields)
            deriving = (await worker.enqueue_derive(conn, profile_id)
                        if maintained else None)
    return {"profile_id": profile_id, "backend": backend,
            "model_version_id": version, "rank": req.rank,
            "roles": [r.model_dump() for r in req.roles],
            "deriving": deriving}


@router.delete("/api/model-profiles/{profile_id}/members/{backend}")
async def delete_model_profile_member(profile_id: int, backend: str):
    """Take a model out of a profile, and queue the re-derive. Its roles
    go with it."""
    async with state.store.acquire() as conn:
        async with conn.transaction():
            gone = await conn.execute(
                "DELETE FROM model_profile_members m USING model_versions v "
                "WHERE v.id = m.model_version_id AND m.profile_id = $1 "
                "AND v.backend = $2", profile_id, backend)
            if not int(gone.split()[-1]):
                raise refuse(404, "member_not_found",
                             f"{backend} is not a member of this profile",
                             backend=backend)
            deriving = (await worker.enqueue_derive(conn, profile_id)
                        if await conn.fetchval(
                            "SELECT maintained FROM model_profiles "
                            "WHERE id = $1", profile_id) else None)
    return {"profile_id": profile_id, "backend": backend, "removed": True,
            "deriving": deriving}


async def _profile_id(conn, name: str | None) -> int:
    if name is None:
        return await conn.fetchval("SELECT active_profile_id()")
    pid = await conn.fetchval(
        "SELECT id FROM model_profiles WHERE name = $1", name)
    if pid is None:
        raise refuse(404, "profile_not_found",
                     f"no model profile named {name!r}", name=name)
    return pid
