-- Tech adoption: one row per language or database that at least 100 people listed.
--
-- Grain: (tech_type, tech_name). Languages and databases are aggregated in
-- separate CTEs, then UNION ALL, so a language never shares a rank list with
-- a database.
--
-- total_users is COUNT(DISTINCT response_id): one person, one vote per tech.
-- want_to_continue_count is a SUM of booleans, not distinct — a duplicated
-- token in one semicolon list would inflate the numerator.
-- retention_rate_pct denominator is users of *that* tech, not all respondents.
--
-- The >= 100 cutoff is an undocumented heuristic. On the Phase A ~12-person
-- fixture this mart is empty; pre-threshold expected counts live in
-- tests/fixtures/expected_mart_tech_adoption_pre_threshold.csv.

WITH lang_stats AS (
    SELECT
        language AS tech_name,
        COUNT(DISTINCT response_id) AS total_users,
        SUM(CASE WHEN wants_to_continue THEN 1 ELSE 0 END) AS want_to_continue_count,
        ROUND(100.0 * SUM(CASE WHEN wants_to_continue THEN 1 ELSE 0 END) / NULLIF(COUNT(DISTINCT response_id), 0), 1) AS retention_rate_pct,
        'Language' AS tech_type
    FROM {{ ref('int_languages_exploded') }}
    GROUP BY language
),
db_stats AS (
    SELECT
        database_name AS tech_name,
        COUNT(DISTINCT response_id) AS total_users,
        SUM(CASE WHEN wants_to_continue THEN 1 ELSE 0 END) AS want_to_continue_count,
        ROUND(100.0 * SUM(CASE WHEN wants_to_continue THEN 1 ELSE 0 END) / NULLIF(COUNT(DISTINCT response_id), 0), 1) AS retention_rate_pct,
        'Database' AS tech_type
    FROM {{ ref('int_databases_exploded') }}
    GROUP BY database_name
),
combined AS (
    SELECT tech_name, total_users, want_to_continue_count, retention_rate_pct, tech_type FROM lang_stats
    UNION ALL
    SELECT tech_name, total_users, want_to_continue_count, retention_rate_pct, tech_type FROM db_stats
)
SELECT
    tech_name,
    total_users,
    want_to_continue_count,
    retention_rate_pct,
    tech_type,
    RANK() OVER (PARTITION BY tech_type ORDER BY total_users DESC) AS usage_rank
FROM combined
WHERE total_users >= 100
ORDER BY tech_type, usage_rank
