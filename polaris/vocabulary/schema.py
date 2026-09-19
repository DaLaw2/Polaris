"""Tables of what people said: concepts, the terms naming them, claims."""

SQL = """
CREATE TABLE IF NOT EXISTS claim_fields (
    field          TEXT PRIMARY KEY,
    multi          BOOLEAN NOT NULL DEFAULT FALSE,
    model_mappable BOOLEAN
);

CREATE TABLE IF NOT EXISTS concepts (
    id         SERIAL PRIMARY KEY,
    kind       TEXT NOT NULL
               CONSTRAINT concepts_kind_fkey REFERENCES claim_fields(field),
    slug       TEXT NOT NULL,
    display_zh TEXT,
    parent_id  INTEGER CONSTRAINT concepts_parent_id_fkey
               REFERENCES concepts(id) ON DELETE RESTRICT,
    note       TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT concepts_kind_slug_key UNIQUE (kind, slug),
    CONSTRAINT concepts_kind_id_key UNIQUE (kind, id)
);
CREATE INDEX IF NOT EXISTS idx_concepts_parent ON concepts (parent_id);

CREATE TABLE IF NOT EXISTS term_map (
    id          BIGSERIAL PRIMARY KEY,
    term        TEXT NOT NULL,
    search_only BOOLEAN NOT NULL DEFAULT FALSE,
    concept_id  INTEGER NOT NULL CONSTRAINT term_map_concept_id_fkey
                REFERENCES concepts(id) ON DELETE RESTRICT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_term_map_unique
    ON term_map (lower(term), concept_id);
CREATE INDEX IF NOT EXISTS idx_term_map_lookup ON term_map (lower(term));
CREATE INDEX IF NOT EXISTS idx_term_map_concept ON term_map (concept_id);

CREATE TABLE IF NOT EXISTS work_claims (
    id         BIGSERIAL PRIMARY KEY,
    work_id    INTEGER NOT NULL CONSTRAINT work_claims_work_id_fkey
               REFERENCES works(id) ON DELETE RESTRICT,
    field      TEXT NOT NULL,
    concept_id INTEGER NOT NULL,
    negated    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT work_claims_concept_fkey FOREIGN KEY (field, concept_id)
        REFERENCES concepts (kind, id) ON DELETE RESTRICT,
    CONSTRAINT work_claims_unique UNIQUE (work_id, field, concept_id)
);
CREATE INDEX IF NOT EXISTS idx_claims_concept ON work_claims (concept_id);

CREATE TABLE IF NOT EXISTS hidden_tags (
    tag TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS tag_translations (
    tag        TEXT PRIMARY KEY,
    display_zh TEXT NOT NULL
);
"""


FUNCTIONS_SQL = """
CREATE OR REPLACE FUNCTION concept_display_zh(p_id INTEGER) RETURNS TEXT
LANGUAGE sql STABLE AS $fn$
    SELECT COALESCE(c.display_zh, (
        SELECT tt.display_zh FROM tag_translations tt
        WHERE tt.tag = c.slug
           OR tt.tag IN (SELECT t.term FROM term_map t
                         WHERE t.concept_id = c.id AND NOT t.search_only)
        ORDER BY (tt.tag = c.slug) DESC, tt.tag
        LIMIT 1))
    FROM concepts c WHERE c.id = p_id
$fn$;
"""


SEED_SQL = """
INSERT INTO claim_fields (field, multi, model_mappable) VALUES
    ('artist',     TRUE,  TRUE),
    ('series',     FALSE, TRUE),
    ('character',  TRUE,  TRUE),
    ('language',   FALSE, FALSE),
    ('rating',     FALSE, FALSE),
    ('color_mode', FALSE, FALSE),
    ('work_type',  FALSE, FALSE),
    ('tag',        TRUE,  FALSE)
ON CONFLICT (field) DO NOTHING;

INSERT INTO concepts (kind, slug, display_zh)
SELECT v.kind, v.slug, v.zh FROM (VALUES
    ('rating',     'sensitive',     '輕度'),
    ('rating',     'questionable',  '中度'),
    ('rating',     'explicit',      '露骨'),
    ('rating',     'general',       '全年齡'),
    ('color_mode', 'partial_color', '部分彩色'),
    ('color_mode', 'full_color',    '全彩'),
    ('color_mode', 'grayscale',     '黑白'),
    ('work_type',  'comic',         '漫畫'),
    ('work_type',  'cg_set',        'CG 集'),
    ('work_type',  'illustration',  '插畫'),
    ('work_type',  'video',         '影片')
) AS v(kind, slug, zh)
ON CONFLICT (kind, slug) DO UPDATE
    SET display_zh = COALESCE(concepts.display_zh, EXCLUDED.display_zh);
"""
