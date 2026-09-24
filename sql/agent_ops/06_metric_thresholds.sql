-- 06_metric_thresholds.sql
-- Fallback breach thresholds for forecasting, used only when an alert's own
-- message does not state one. Matched as a substring of alert_type (longest
-- pattern wins). Policy config, like sla_policy: edit and re-run the loader.
CREATE TABLE IF NOT EXISTS `__PROJECT_ID__.sre_agent_ops.metric_thresholds`
(
  pattern STRING OPTIONS(description="Lowercase substring of alert_type, e.g. 'disk'."),
  threshold FLOAT64,
  unit STRING,
  notes STRING
);

MERGE INTO `__PROJECT_ID__.sre_agent_ops.metric_thresholds` AS T
USING (
  SELECT * FROM UNNEST(ARRAY<STRUCT<pattern STRING, threshold FLOAT64, unit STRING, notes STRING>>[
    ('disk',            95.0, '%', 'Filesystem usage; kubelet evicts pods near full.'),
    ('memory',          90.0, '%', 'Working set; OOM kills follow.'),
    ('cpu',             95.0, '%', 'Sustained saturation.'),
    ('session_pool',    95.0, '%', 'Session/connection pool utilisation.'),
    ('connection_pool', 95.0, '%', 'Connection pool utilisation.')
  ])
) AS S
ON T.pattern = S.pattern
WHEN MATCHED THEN UPDATE SET threshold = S.threshold, unit = S.unit, notes = S.notes
WHEN NOT MATCHED THEN INSERT (pattern, threshold, unit, notes) VALUES (S.pattern, S.threshold, S.unit, S.notes);
