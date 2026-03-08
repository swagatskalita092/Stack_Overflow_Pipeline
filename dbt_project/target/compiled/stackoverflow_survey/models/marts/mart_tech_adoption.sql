WITH lang_stats AS (
    SELECT
        language AS tech_name,
        COUNT(DISTINCT response_id) AS total_users,
        SUM(CASE WHEN wants_to_continue THEN 1 ELSE 0 END) AS want_to_continue_count,
        ROUND(100.0 * SUM(CASE WHEN wants_to_continue THEN 1 ELSE 0 END) / NULLIF(COUNT(DISTINCT response_id), 0), 1) AS retention_rate_pct,
        'Language' AS tech_type
    FROM "survey_db"."dwh_intermediate"."int_languages_exploded"
    GROUP BY language
),
db_stats AS (
    SELECT
        database_name AS tech_name,
        COUNT(DISTINCT response_id) AS total_users,
        SUM(CASE WHEN wants_to_continue THEN 1 ELSE 0 END) AS want_to_continue_count,
        ROUND(100.0 * SUM(CASE WHEN wants_to_continue THEN 1 ELSE 0 END) / NULLIF(COUNT(DISTINCT response_id), 0), 1) AS retention_rate_pct,
        'Database' AS tech_type
    FROM "survey_db"."dwh_intermediate"."int_databases_exploded"
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