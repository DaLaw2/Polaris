"""Derived tables, the views over them, and the SQL that fills them."""

SQL = """
CREATE TABLE IF NOT EXISTS derived.work_vectors (
    model_version_id SMALLINT NOT NULL REFERENCES model_versions(id),
    work_id          INTEGER  NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    vec              vector   NOT NULL,
    PRIMARY KEY (model_version_id, work_id)
);
CREATE TABLE IF NOT EXISTS derivation_params (
    name  TEXT PRIMARY KEY,
    value NUMERIC NOT NULL
);
CREATE TABLE IF NOT EXISTS rating_order (
    name TEXT PRIMARY KEY,
    rank INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS derivation_tag_sets (
    set_name TEXT NOT NULL,
    tag      TEXT NOT NULL,
    PRIMARY KEY (set_name, tag)
);
"""


FUNCTIONS_SQL = """
CREATE OR REPLACE FUNCTION coverage_lower_bound(
    hits NUMERIC, samples NUMERIC, z NUMERIC
) RETURNS DOUBLE PRECISION LANGUAGE sql IMMUTABLE AS $fn$
    SELECT CASE
        WHEN samples <= 0 THEN 0::double precision
        WHEN z = 0 THEN (hits / samples)::double precision
        ELSE (
            (hits / samples)::double precision
            + z::double precision * z::double precision
              / (2 * samples::double precision)
            - z::double precision * sqrt(
                (hits / samples)::double precision
                * (1 - hits / samples)::double precision
                / samples::double precision
                + z::double precision * z::double precision
                  / (4 * samples::double precision * samples::double precision))
        ) / (1 + z::double precision * z::double precision
                 / samples::double precision)
    END
$fn$;
"""


TABLES_SQL = """
CREATE SCHEMA IF NOT EXISTS derived;

CREATE TABLE IF NOT EXISTS derived.work_tags (
    profile_id SMALLINT NOT NULL REFERENCES model_profiles(id) ON DELETE CASCADE,
    work_id    INTEGER  NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    category   TEXT     NOT NULL,
    tag        TEXT     NOT NULL,
    freq_ratio REAL     NOT NULL,
    avg_score  REAL     NOT NULL,
    PRIMARY KEY (profile_id, work_id, category, tag)
);
CREATE INDEX IF NOT EXISTS idx_work_tags_tag ON derived.work_tags (profile_id, tag);
CREATE INDEX IF NOT EXISTS idx_work_tags_cat
    ON derived.work_tags (profile_id, category, tag);

CREATE TABLE IF NOT EXISTS derived.work_model_claims (
    profile_id SMALLINT NOT NULL REFERENCES model_profiles(id) ON DELETE CASCADE,
    work_id    INTEGER  NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    field      TEXT     NOT NULL,
    concept_id INTEGER  NOT NULL,
    PRIMARY KEY (profile_id, work_id, field, concept_id),
    FOREIGN KEY (field, concept_id) REFERENCES concepts (kind, id)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_work_model_claims_concept
    ON derived.work_model_claims (profile_id, field, concept_id);

CREATE TABLE IF NOT EXISTS derived.work_search (
    profile_id   SMALLINT NOT NULL REFERENCES model_profiles(id) ON DELETE CASCADE,
    id           INTEGER  NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    run_id       BIGINT,
    folder_path  TEXT,
    folder_name  TEXT,
    collection   TEXT,
    title        TEXT,
    artist       TEXT,
    series       TEXT,
    language     TEXT,
    rating       TEXT,
    color_mode   TEXT,
    work_type    TEXT,
    total_images INTEGER,
    has_video    BOOLEAN,
    duration_s   NUMERIC,
    analyzed_at  TIMESTAMPTZ,
    PRIMARY KEY (profile_id, id)
);
"""


VIEWS_SQL = """
CREATE OR REPLACE VIEW work_tags AS
SELECT work_id, category, tag, freq_ratio, avg_score
FROM derived.work_tags WHERE profile_id = (SELECT active_profile_id());

CREATE OR REPLACE VIEW work_model_claims AS
SELECT work_id, field, concept_id
FROM derived.work_model_claims WHERE profile_id = (SELECT active_profile_id());

CREATE OR REPLACE VIEW work_search AS
SELECT id, run_id, folder_path, folder_name, collection, title, artist, series,
       language, rating, color_mode, work_type, total_images, has_video,
       duration_s, analyzed_at
FROM derived.work_search WHERE profile_id = (SELECT active_profile_id());

CREATE OR REPLACE VIEW work_all_claims AS
SELECT c.work_id, c.field, c.concept_id, c.negated, 2 AS priority,
       c.created_at, c.id AS claim_id
FROM work_claims c
UNION ALL
SELECT m.work_id, m.field, m.concept_id, FALSE, 1, NULL::timestamptz,
       NULL::bigint
FROM work_model_claims m;

CREATE OR REPLACE VIEW work_claims_ranked AS
SELECT a.work_id, a.field, a.concept_id, co.slug AS value, a.priority,
       a.created_at, a.claim_id, f.multi
FROM work_all_claims a
JOIN concepts co ON co.id = a.concept_id
JOIN claim_fields f ON f.field = a.field
WHERE NOT a.negated
  AND NOT EXISTS (SELECT 1 FROM work_claims n
                  WHERE n.negated AND n.work_id = a.work_id
                    AND n.field = a.field AND n.concept_id = a.concept_id);

CREATE OR REPLACE VIEW work_claims_current AS
SELECT DISTINCT ON (r.work_id, r.field)
       r.work_id, r.field, r.concept_id, r.value, r.priority, r.created_at
FROM work_claims_ranked r
ORDER BY r.work_id, r.field, r.priority DESC, r.created_at DESC NULLS LAST,
         r.claim_id DESC NULLS LAST, r.value;

CREATE OR REPLACE VIEW work_claims_multi AS
SELECT DISTINCT r.work_id, r.field, r.concept_id, r.value
FROM work_claims_ranked r
WHERE r.multi;

CREATE OR REPLACE VIEW effective_tags AS
SELECT t.work_id, t.category, t.tag,
       (t.freq_ratio * t.avg_score)::real AS confidence,
       t.freq_ratio, t.avg_score, h.tag IS NULL AS visible
FROM work_tags t
LEFT JOIN hidden_tags h ON h.tag = t.tag
WHERE t.category <> 'rating'
  AND NOT EXISTS (
      SELECT 1 FROM work_claims n
      JOIN concepts c ON c.id = n.concept_id
      WHERE n.work_id = t.work_id
        AND n.field = CASE t.category WHEN 'general' THEN 'tag'
                                      WHEN 'copyright' THEN 'series'
                                      ELSE t.category END
        AND n.negated
        AND (lower(c.slug) = lower(t.tag)
             OR EXISTS (SELECT 1 FROM term_map tm
                        WHERE tm.concept_id = c.id AND NOT tm.search_only
                          AND lower(tm.term) = lower(t.tag))))
UNION ALL
SELECT c.work_id,
       CASE c.field WHEN 'tag' THEN 'general' ELSE c.field END,
       co.slug, 1.0::real, 1.0::real, 1.0::real, h.tag IS NULL
FROM work_claims c
JOIN concepts co ON co.id = c.concept_id
LEFT JOIN hidden_tags h ON h.tag = co.slug
WHERE NOT c.negated AND c.field IN ('tag', 'character', 'artist')
  AND NOT EXISTS (
      SELECT 1 FROM work_tags t
      WHERE t.work_id = c.work_id AND t.tag = co.slug
        AND t.category = CASE c.field WHEN 'tag' THEN 'general'
                                      ELSE c.field END);

CREATE OR REPLACE VIEW work_identity AS
SELECT w.id,
       pp.path       AS folder_path,
       pp.collection AS collection,
       reverse(split_part(reverse(replace(pp.path, '/', '\\')), '\\', 1))
           AS folder_name
FROM works w
LEFT JOIN LATERAL (
    SELECT path, collection
    FROM work_paths
    WHERE work_id = w.id
    ORDER BY present DESC, last_seen DESC, id DESC
    LIMIT 1
) pp ON TRUE;

CREATE OR REPLACE VIEW works_effective AS
SELECT w.id, s.folder_path, s.folder_name, s.collection, s.analyzed_at,
       s.title, s.artist, s.series, s.language, s.rating, s.color_mode,
       s.work_type, s.total_images, s.has_video, s.duration_s
FROM works w
LEFT JOIN work_search s ON s.id = w.id;
"""


DROP_VIEWS_SQL = """
DROP VIEW IF EXISTS works_effective     CASCADE;
DROP VIEW IF EXISTS work_identity       CASCADE;
DROP VIEW IF EXISTS effective_tags      CASCADE;
DROP VIEW IF EXISTS work_claims_multi   CASCADE;
DROP VIEW IF EXISTS work_claims_current CASCADE;
DROP VIEW IF EXISTS work_claims_ranked  CASCADE;
DROP VIEW IF EXISTS work_all_claims     CASCADE;
DROP VIEW IF EXISTS work_search         CASCADE;
DROP VIEW IF EXISTS work_model_claims   CASCADE;
DROP VIEW IF EXISTS work_tags           CASCADE;
"""


_CURRENT_CTES = """
    cur AS (
        SELECT id AS work_id, run_id FROM derived.work_search
        WHERE profile_id = p_profile AND id = ANY(p_ids) AND run_id IS NOT NULL
    ),
    stats AS (
        SELECT s.work_id,
               COUNT(*) FILTER (WHERE s.analyzed)::int AS analyzed_samples,
               bool_or(s.medium = 'video') AS has_video,
               bool_or(s.medium = 'image') AS has_image
        FROM cur
        JOIN work_samples s
          ON s.work_id = cur.work_id AND s.run_id = cur.run_id
        GROUP BY s.work_id
    ),
    measures AS (
        SELECT m.work_id,
               MAX((m.value #>> '{}')::numeric)
                   FILTER (WHERE m.kind = 'duration_s') AS duration_s,
               MAX((m.value ->> 'files')::int)
                   FILTER (WHERE m.kind = 'page_census') AS page_files,
               (COUNT(*) FILTER (
                    WHERE m.kind = 'color_ratio'
                      AND (m.value #>> '{}')::numeric
                          > (SELECT derivation_param(p_profile, 'color:page_ratio')))
               )::numeric
               / NULLIF(COUNT(*) FILTER (WHERE m.kind = 'color_ratio'), 0)
                   AS color_page_ratio,
               (array_agg(m.value) FILTER (WHERE m.kind = 'cg_features'))[1]
                   AS cg_features,
               (array_agg(m.value) FILTER (WHERE m.kind = 'video_probe'))[1]
                   AS video_probe
        FROM cur
        JOIN measurements m
          ON m.work_id = cur.work_id AND m.run_id = cur.run_id
        GROUP BY m.work_id
    )"""


_CUTOFFS = """cutoffs AS MATERIALIZED (
        SELECT r.model_version_id, r.role AS category, r.yields,
               COALESCE(r.score_cutoff,
                        (SELECT value FROM model_profile_params
                         WHERE profile_id = p_profile
                           AND name = 'score:' || r.role),
                        (SELECT derivation_param(p_profile, 'score:default')))
                   AS cutoff,
               CASE WHEN r.score_cutoff IS NOT NULL THEN 'member'
                    WHEN EXISTS (SELECT 1 FROM model_profile_params
                                 WHERE profile_id = p_profile
                                   AND name = 'score:' || r.role)
                    THEN 'score:' || r.role
                    ELSE 'score:default' END AS cutoff_source
        FROM model_profile_roles r
        WHERE r.profile_id = p_profile AND r.role <> 'embedding'
    )"""

_GATE_FROM = """
        JOIN cutoffs k
          ON k.model_version_id = f.model_version_id AND k.category = f.category
        LEFT JOIN derived.tag_thresholds tt
               ON tt.model_version_id = f.model_version_id
              AND tt.category = f.category AND tt.tag = f.tag"""

_GATE_THRESHOLD = "COALESCE(tt.threshold, k.cutoff)"

_YIELDS_TO = """
                  SELECT o.model_version_id FROM model_profile_roles o
                  JOIN derived.tag_thresholds ot
                    ON ot.model_version_id = o.model_version_id
                   AND ot.category = o.role AND ot.tag = f.tag
                  WHERE o.profile_id = p_profile
                    AND o.role = f.category
                    AND o.model_version_id <> f.model_version_id"""

_GATE_OPEN = "(NOT k.yields OR NOT EXISTS (" + _YIELDS_TO + "))"

_FREQ_FROM = """
    LEFT JOIN model_profile_params pf
           ON pf.profile_id = p_profile AND pf.name = 'freq:' || a.category
    LEFT JOIN model_profile_params pz
           ON pz.profile_id = p_profile AND pz.name = 'freqz:' || a.category"""

_FREQ_MIN = "COALESCE(pf.value, (SELECT derivation_param(p_profile, 'freq:default')))"

_FREQ_Z = "COALESCE(pz.value, (SELECT derivation_param(p_profile, 'freqz:default')))"


DERIVE_SQL = """
DROP FUNCTION IF EXISTS derive_work_tags(INTEGER[]);
DROP FUNCTION IF EXISTS derivation_param(TEXT);
DROP FUNCTION IF EXISTS derivation_param(TEXT, TEXT);

CREATE OR REPLACE FUNCTION derive_work_tags(p_profile SMALLINT, p_ids INTEGER[])
RETURNS VOID LANGUAGE plpgsql SET enable_nestloop = off AS $fn$
BEGIN
    DELETE FROM derived.work_tags
    WHERE profile_id = p_profile AND work_id = ANY(p_ids);

    INSERT INTO derived.work_tags
        (profile_id, work_id, category, tag, freq_ratio, avg_score)
    WITH pages AS MATERIALIZED (
        SELECT s.id AS sample_id, s.work_id,
               dense_rank() OVER (PARTITION BY s.work_id
                                  ORDER BY s.medium, s.source_relpath,
                                           s.position) AS page
        FROM work_samples s
        WHERE s.work_id = ANY(p_ids) AND s.analyzed
    ),
    chosen AS MATERIALIZED (
        SELECT DISTINCT ON (p.work_id, v.backend) p.work_id, ss.model_version_id
        FROM pages p
        JOIN sample_scores ss ON ss.sample_id = p.sample_id
        JOIN model_profile_members m
          ON m.profile_id = p_profile AND m.model_version_id = ss.model_version_id
        JOIN model_versions v ON v.id = m.model_version_id
        ORDER BY p.work_id, v.backend, m.rank
    ),
    seen AS MATERIALIZED (
        SELECT c.work_id, c.model_version_id, p.page, ss.tag_ids, ss.scores
        FROM chosen c
        JOIN pages p ON p.work_id = c.work_id
        JOIN sample_scores ss
          ON ss.sample_id = p.sample_id AND ss.model_version_id = c.model_version_id
    ),
    flat AS MATERIALIZED (
        SELECT x.work_id, x.page, x.model_version_id, mt.category,
               mt.name AS tag, u.score
        FROM seen x
        CROSS JOIN LATERAL unnest(x.tag_ids, x.scores) AS u(tag_id, score)
        JOIN model_tags mt ON mt.id = u.tag_id
        WHERE (x.model_version_id, mt.category) IN (
            SELECT model_version_id, role FROM model_profile_roles
            WHERE profile_id = p_profile)
    ),
    contributors AS (
        SELECT c.work_id, r.role AS category, c.model_version_id
        FROM chosen c
        JOIN model_profile_roles r
          ON r.profile_id = p_profile AND r.model_version_id = c.model_version_id
        WHERE r.role <> 'embedding'
    ),
    """ + _CUTOFFS + """,
    denom AS (
        SELECT cb.work_id, cb.category, COUNT(DISTINCT sn.page)::numeric AS samples
        FROM contributors cb
        JOIN seen sn
          ON sn.work_id = cb.work_id AND sn.model_version_id = cb.model_version_id
        GROUP BY cb.work_id, cb.category
    ),
    scored AS (
        SELECT f.work_id, f.page, f.category, f.tag, f.score
        FROM flat f""" + _GATE_FROM + """
        WHERE f.score >= """ + _GATE_THRESHOLD + """
          AND """ + _GATE_OPEN + """
    ),
    agg AS (
        SELECT work_id, category, tag,
               COUNT(DISTINCT page)::numeric AS hits,
               AVG(score)::real AS avg_score
        FROM scored
        GROUP BY work_id, category, tag
    )
    SELECT p_profile, a.work_id, a.category, a.tag,
           (a.hits / d.samples)::real, a.avg_score
    FROM agg a
    JOIN denom d ON d.work_id = a.work_id AND d.category = a.category
""" + _FREQ_FROM + """
    WHERE coverage_lower_bound(a.hits, d.samples, """ + _FREQ_Z + """)
          >= """ + _FREQ_MIN + """;
END
$fn$;

CREATE OR REPLACE FUNCTION derive_profile(p_profile SMALLINT, p_ids INTEGER[])
RETURNS VOID LANGUAGE plpgsql SET enable_nestloop = off AS $fn$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM model_profiles
                   WHERE id = p_profile AND maintained) THEN
        RETURN;
    END IF;
    PERFORM 1 FROM works WHERE id = ANY(p_ids) ORDER BY id FOR NO KEY UPDATE;

    DELETE FROM derived.work_search ws
    WHERE ws.profile_id = p_profile AND ws.id = ANY(p_ids)
      AND NOT EXISTS (SELECT 1 FROM works w WHERE w.id = ws.id);

    INSERT INTO derived.work_search (profile_id, id, run_id)
    SELECT p_profile, w.id, ev.run_id
    FROM works w
    LEFT JOIN (
        SELECT p.work_id, MAX(p.run_id) AS run_id
        FROM (
            SELECT work_id, run_id, bool_or(analyzed) AS looked
            FROM work_samples WHERE work_id = ANY(p_ids)
            GROUP BY work_id, run_id
            UNION ALL
            SELECT m.work_id, m.run_id, FALSE
            FROM measurements m
            WHERE m.work_id = ANY(p_ids)
              AND NOT EXISTS (SELECT 1 FROM work_samples s
                              WHERE s.work_id = m.work_id
                                AND s.run_id = m.run_id)
            GROUP BY m.work_id, m.run_id
        ) p
        WHERE NOT p.looked
           OR EXISTS (SELECT 1 FROM work_samples s
                      JOIN sample_scores ss ON ss.sample_id = s.id
                      JOIN model_profile_members mb
                        ON mb.model_version_id = ss.model_version_id
                       AND mb.profile_id = p_profile
                      WHERE s.work_id = p.work_id AND s.run_id = p.run_id
                        AND s.analyzed)
        GROUP BY p.work_id
    ) ev ON ev.work_id = w.id
    WHERE w.id = ANY(p_ids)
    ON CONFLICT (profile_id, id) DO UPDATE SET run_id = EXCLUDED.run_id;

    PERFORM derive_work_tags(p_profile, p_ids);

    DELETE FROM derived.work_model_claims
    WHERE profile_id = p_profile AND work_id = ANY(p_ids);

    INSERT INTO derived.work_model_claims (profile_id, work_id, field, concept_id)
    WITH""" + _CURRENT_CTES + """,
    rating_members AS (
        SELECT r.model_version_id, m.rank
        FROM model_profile_roles r
        JOIN model_profile_members m
          ON m.profile_id = r.profile_id AND m.model_version_id = r.model_version_id
        WHERE r.profile_id = p_profile AND r.role = 'rating'
    ),
    rating_chosen AS (
        SELECT DISTINCT ON (s.work_id) s.work_id, ss.model_version_id
        FROM work_samples s
        JOIN sample_scores ss ON ss.sample_id = s.id
        JOIN rating_members rm ON rm.model_version_id = ss.model_version_id
        WHERE s.work_id = ANY(p_ids) AND s.analyzed
        ORDER BY s.work_id, rm.rank
    ),
    top_rating AS (
        SELECT DISTINCT ON (s.id) s.work_id, s.id AS sample_id, mt.name AS tag
        FROM rating_chosen rc
        JOIN work_samples s ON s.work_id = rc.work_id AND s.analyzed
        JOIN sample_scores ss
          ON ss.sample_id = s.id AND ss.model_version_id = rc.model_version_id
        CROSS JOIN LATERAL unnest(ss.tag_ids, ss.scores) AS u(tag_id, score)
        JOIN model_tags mt ON mt.id = u.tag_id
        WHERE mt.category = 'rating'
          AND u.score >= (SELECT derivation_param(p_profile, 'rating:score'))
        ORDER BY s.id, u.score DESC, mt.name
    ),
    rating AS (
        SELECT work_id, 'rating'::text AS field, name AS value
        FROM (
            SELECT tr.work_id, ro.name, ro.rank, COUNT(*) AS pages,
                   ROW_NUMBER() OVER (PARTITION BY tr.work_id
                                      ORDER BY ro.rank DESC, ro.name) AS rn
            FROM top_rating tr
            JOIN rating_order ro ON ro.name = tr.tag
            GROUP BY tr.work_id, ro.name, ro.rank
        ) r
        WHERE pages >= (SELECT derivation_param(p_profile, 'rating:min_pages'))
          AND rn = 1
    ),
    color AS (
        SELECT work_id, 'color_mode'::text AS field,
               CASE
                 WHEN color_page_ratio
                      >= (SELECT derivation_param(p_profile, 'color:full'))
                   THEN 'full_color'
                 WHEN color_page_ratio
                      <= (SELECT derivation_param(p_profile, 'color:gray'))
                   THEN 'grayscale'
                 ELSE 'partial_color'
               END AS value
        FROM measures
        WHERE color_page_ratio IS NOT NULL
    ),
    tags AS (
        SELECT * FROM derived.work_tags
        WHERE profile_id = p_profile AND work_id = ANY(p_ids)
    ),
    comic AS (
        SELECT work_id, freq_ratio * avg_score AS score
        FROM tags WHERE category = 'general' AND tag = 'comic'
    ),
    illust AS (
        SELECT t.work_id, SUM(t.freq_ratio * t.avg_score) AS score
        FROM tags t
        JOIN derivation_tag_sets ds
          ON ds.set_name = 'illustration_signal' AND ds.tag = t.tag
        WHERE t.category = 'general'
        GROUP BY t.work_id
    ),
    observed AS (
        SELECT COALESCE(st.work_id, wm.work_id) AS work_id,
               st.has_video, st.has_image, wm.video_probe, wm.cg_features
        FROM stats st
        FULL JOIN measures wm ON wm.work_id = st.work_id
    ),
    wtype AS (
        SELECT o.work_id, 'work_type'::text AS field,
               CASE
                 WHEN COALESCE(o.has_video, o.video_probe IS NOT NULL, FALSE)
                  AND NOT COALESCE(o.has_image, FALSE)
                   THEN 'video'
                 WHEN COALESCE(c.score, 0)
                      >= (SELECT derivation_param(p_profile, 'type:comic'))
                   THEN 'comic'
                 WHEN (o.cg_features ->> 'n_images')::numeric
                        BETWEEN (SELECT derivation_param(p_profile, 'cg:min_images'))
                            AND (SELECT derivation_param(p_profile, 'cg:max_images'))
                  AND (o.cg_features ->> 'sequential_ratio')::numeric
                        >= (SELECT derivation_param(p_profile, 'cg:seq_ratio'))
                  AND (o.cg_features ->> 'aspect_std')::numeric
                        <= (SELECT derivation_param(p_profile, 'cg:aspect_std'))
                  AND (o.cg_features ->> 'avg_file_size')::numeric
                        >= (SELECT derivation_param(p_profile, 'cg:min_avg_size'))
                   THEN 'cg_set'
                 WHEN COALESCE(i.score, 0)
                      >= (SELECT derivation_param(p_profile, 'type:illust'))
                   THEN 'illustration'
               END AS value
        FROM observed o
        LEFT JOIN comic  c ON c.work_id = o.work_id
        LEFT JOIN illust i ON i.work_id = o.work_id
    ),
    named AS (
        SELECT DISTINCT t.work_id, c.kind AS field, c.id AS concept_id
        FROM tags t
        JOIN term_map tm ON lower(tm.term) = lower(t.tag) AND NOT tm.search_only
        JOIN concepts c ON c.id = tm.concept_id AND c.kind = t.category
        JOIN claim_fields f ON f.field = c.kind AND f.model_mappable
        WHERE c.kind <> 'series'
    ),
    character_signal AS (
        SELECT t.work_id, ch.id AS character_id,
               MAX(t.freq_ratio * t.avg_score) AS score, 0 AS by_user
        FROM tags t
        JOIN term_map tm ON lower(tm.term) = lower(t.tag) AND NOT tm.search_only
        JOIN concepts ch ON ch.id = tm.concept_id AND ch.kind = 'character'
        WHERE t.category = 'character'
        GROUP BY t.work_id, ch.id
        UNION ALL
        SELECT c.work_id, c.concept_id, 1.0, 1
        FROM work_claims c
        WHERE c.work_id = ANY(p_ids) AND c.field = 'character' AND NOT c.negated
    ),
    franchise AS (
        SELECT DISTINCT ON (f.work_id) f.work_id, 'series'::text AS field,
               f.concept_id
        FROM (
            SELECT x.work_id, parent.id AS concept_id, parent.slug,
                   MAX(x.by_user) AS by_user, MAX(x.score) AS score
            FROM character_signal x
            JOIN concepts ch ON ch.id = x.character_id
            JOIN concepts parent ON parent.id = ch.parent_id
                                AND parent.kind = 'series'
            WHERE NOT EXISTS (SELECT 1 FROM work_claims n
                              WHERE n.work_id = x.work_id AND n.negated
                                AND n.field = 'character'
                                AND n.concept_id = x.character_id)
              AND NOT EXISTS (SELECT 1 FROM work_claims n
                              WHERE n.work_id = x.work_id AND n.negated
                                AND n.field = 'series'
                                AND n.concept_id = parent.id)
            GROUP BY x.work_id, parent.id, parent.slug
        ) f
        ORDER BY f.work_id, f.by_user DESC, f.score DESC, f.slug
    ),
    enums AS (
        SELECT work_id, field, value FROM rating
        UNION ALL SELECT work_id, field, value FROM color
        UNION ALL SELECT work_id, field, value FROM wtype WHERE value IS NOT NULL
    )
    SELECT p_profile, e.work_id, e.field, c.id
    FROM enums e JOIN concepts c ON c.kind = e.field AND c.slug = e.value
    UNION ALL SELECT p_profile, work_id, field, concept_id FROM franchise
    UNION ALL SELECT p_profile, work_id, field, concept_id FROM named;

    WITH""" + _CURRENT_CTES + """,
    claims AS (
        SELECT c.work_id, c.field, c.concept_id, 2 AS priority, c.created_at,
               c.id AS claim_id
        FROM work_claims c
        WHERE c.work_id = ANY(p_ids) AND NOT c.negated
        UNION ALL
        SELECT m.work_id, m.field, m.concept_id, 1, NULL::timestamptz,
               NULL::bigint
        FROM derived.work_model_claims m
        WHERE m.profile_id = p_profile AND m.work_id = ANY(p_ids)
          AND NOT EXISTS (SELECT 1 FROM work_claims n
                          WHERE n.negated AND n.work_id = m.work_id
                            AND n.field = m.field
                            AND n.concept_id = m.concept_id)
    ),
    current_claims AS (
        SELECT DISTINCT ON (x.work_id, x.field) x.work_id, x.field,
               co.slug AS value
        FROM claims x
        JOIN concepts co ON co.id = x.concept_id
        ORDER BY x.work_id, x.field, x.priority DESC,
                 x.created_at DESC NULLS LAST, x.claim_id DESC NULLS LAST,
                 co.slug
    ),
    pivot AS (
        SELECT work_id,
               MAX(value) FILTER (WHERE field = 'artist')     AS artist,
               MAX(value) FILTER (WHERE field = 'series')     AS series,
               MAX(value) FILTER (WHERE field = 'language')   AS language,
               MAX(value) FILTER (WHERE field = 'rating')     AS rating,
               MAX(value) FILTER (WHERE field = 'color_mode') AS color_mode,
               MAX(value) FILTER (WHERE field = 'work_type')  AS work_type
        FROM current_claims
        GROUP BY work_id
    ),
    last_seen AS (
        SELECT s.work_id, MAX(r.started_at) AS analyzed_at
        FROM work_samples s JOIN scan_runs r ON r.id = s.run_id
        WHERE s.work_id = ANY(p_ids)
        GROUP BY s.work_id
    ),
    ident AS (
        SELECT * FROM work_identity WHERE id = ANY(p_ids)
    )
    UPDATE derived.work_search ws SET
        folder_path  = i.folder_path,
        folder_name  = i.folder_name,
        collection   = i.collection,
        title        = COALESCE(w.title, i.folder_name),
        analyzed_at  = COALESCE(ls.analyzed_at, w.created_at),
        artist       = p.artist,
        series       = p.series,
        language     = p.language,
        rating       = p.rating,
        color_mode   = p.color_mode,
        work_type    = p.work_type,
        total_images = COALESCE(ms.page_files, st.analyzed_samples, 0)::int,
        has_video    = COALESCE(st.has_video, ms.video_probe IS NOT NULL, FALSE),
        duration_s   = ms.duration_s
    FROM works w
    LEFT JOIN ident     i  ON i.id = w.id
    LEFT JOIN pivot     p  ON p.work_id = w.id
    LEFT JOIN stats     st ON st.work_id = w.id
    LEFT JOIN measures  ms ON ms.work_id = w.id
    LEFT JOIN last_seen ls ON ls.work_id = w.id
    WHERE ws.profile_id = p_profile AND ws.id = w.id AND w.id = ANY(p_ids);
END
$fn$;

CREATE OR REPLACE FUNCTION derive_works(p_ids INTEGER[]) RETURNS VOID
LANGUAGE plpgsql AS $fn$
DECLARE p SMALLINT;
BEGIN
    PERFORM 1 FROM works WHERE id = ANY(p_ids) ORDER BY id FOR NO KEY UPDATE;
    FOR p IN SELECT id FROM model_profiles WHERE maintained ORDER BY id LOOP
        PERFORM derive_profile(p, p_ids);
    END LOOP;
END
$fn$;

DROP FUNCTION IF EXISTS explain_tag(SMALLINT, TEXT);
CREATE FUNCTION explain_tag(p_profile SMALLINT, p_tag TEXT)
RETURNS TABLE (model_version_id SMALLINT, category TEXT, official NUMERIC,
               threshold NUMERIC, source TEXT, yields BOOLEAN,
               applies BOOLEAN, yields_to SMALLINT[], freq NUMERIC,
               freq_source TEXT, freqz NUMERIC, freqz_source TEXT)
LANGUAGE sql STABLE AS $fn$
    WITH """ + _CUTOFFS + """
    SELECT f.model_version_id, f.category, tt.threshold,
           """ + _GATE_THRESHOLD + """,
           CASE WHEN tt.threshold IS NOT NULL THEN 'official'
                ELSE k.cutoff_source END,
           k.yields,
           """ + _GATE_OPEN + """,
           ARRAY(""" + _YIELDS_TO + """
                 ORDER BY o.model_version_id),
           """ + _FREQ_MIN + """,
           COALESCE(pf.name, 'freq:default'),
           """ + _FREQ_Z + """,
           COALESCE(pz.name, 'freqz:default')
    FROM (SELECT r.model_version_id, r.role AS category, p_tag AS tag
          FROM model_profile_roles r
          WHERE r.profile_id = p_profile AND r.role <> 'embedding') f
    CROSS JOIN LATERAL (SELECT f.category) a(category)""" + _GATE_FROM + (
    _FREQ_FROM) + """
$fn$;
"""


SEED_SQL = """
INSERT INTO rating_order (name, rank) VALUES
    ('general', 1), ('sensitive', 2), ('questionable', 3), ('explicit', 4)
ON CONFLICT (name) DO NOTHING;

INSERT INTO derivation_tag_sets (set_name, tag) VALUES
    ('illustration_signal', '1girl'),
    ('illustration_signal', 'solo'),
    ('illustration_signal', 'full_body'),
    ('illustration_signal', 'standing'),
    ('illustration_signal', 'sitting')
ON CONFLICT (set_name, tag) DO NOTHING;
"""
