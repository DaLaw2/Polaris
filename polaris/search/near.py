"""Works in the library that look alike, grouped around one work each."""

from __future__ import annotations

import asyncio
import time

import numpy as np

from polaris.models import profiles

CHUNK = 1024
TTL = 60.0

_cache: dict[tuple[int, float], tuple[float, dict[int, list[tuple[int, float]]]]] = {}


def group(ids: list[int], vectors: np.ndarray,
          min_score: float) -> dict[int, list[tuple[int, float]]]:
    """Leader id -> [(member id, similarity)], strongest pairs first.

    A work joins a group only by its likeness to that group's leader, so
    a run of works each a little like the next never chains into one.
    """
    x = vectors / np.linalg.norm(vectors, axis=1, keepdims=True)
    pairs = []
    for start in range(0, len(ids), CHUNK):
        s = x[start:start + CHUNK] @ x.T
        rows, cols = np.nonzero(s >= min_score)
        keep = cols > rows + start
        pairs.extend(zip(s[rows[keep], cols[keep]].tolist(),
                         (rows[keep] + start).tolist(), cols[keep].tolist()))
    pairs.sort(reverse=True)

    groups: dict[int, list[tuple[int, float]]] = {}
    member: set[int] = set()
    for score, i, j in pairs:
        free_i = i not in groups and i not in member
        free_j = j not in groups and j not in member
        if free_i and free_j:
            groups[i] = [(j, score)]
            member.add(j)
        elif i in groups and free_j:
            groups[i].append((j, score))
            member.add(j)
        elif j in groups and free_i:
            groups[j].append((i, score))
            member.add(i)
    return {ids[a]: [(ids[b], s) for b, s in m] for a, m in groups.items()}


async def groups(conn, min_score: float) -> dict[int, list[tuple[int, float]]]:
    """Near-duplicate groups among works on disk, by the active profile's
    work vectors."""
    version = await profiles.embedding_version(conn)
    if version is None:
        return {}
    key = (version, min_score)
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < TTL:
        return hit[1]
    rows = await conn.fetch(
        "SELECT v.work_id, v.vec::real[] AS vec FROM derived.work_vectors v "
        "WHERE v.model_version_id = $1 AND EXISTS ("
        "  SELECT 1 FROM work_paths p WHERE p.work_id = v.work_id AND p.present)"
        " ORDER BY v.work_id", version)
    if not rows:
        return {}
    ids = [r["work_id"] for r in rows]
    vectors = np.asarray([r["vec"] for r in rows], dtype=np.float32)
    found = await asyncio.to_thread(group, ids, vectors, min_score)
    _cache[key] = (time.monotonic(), found)
    return found
