-- 02_seed_sla_policy.sql
-- ASSUMPTION: the kit defines the credit formula but not the per-tier rates or
-- restoration targets. These values are our documented policy defaults; change
-- them here (and re-run the loader) if the organisers publish official ones.
-- The only constraint taken from the kit: GOLD is the only tier with a
-- sub-hour restoration commitment.
MERGE INTO `__PROJECT_ID__.sre_agent_ops.sla_policy` AS T
USING (
  SELECT * FROM UNNEST(ARRAY<STRUCT<
    tier STRING, restoration_target_minutes INT64, credit_rate FLOAT64, notes STRING
  >>[
    ('GOLD',    60, 10.0, 'Sub-hour restoration commitment; highest credit multiplier.'),
    ('SILVER', 240,  5.0, 'Four-hour restoration target.'),
    ('BRONZE', 480,  2.0, 'Business-day restoration target.')
  ])
) AS S
ON T.tier = S.tier
WHEN MATCHED THEN UPDATE SET
  restoration_target_minutes = S.restoration_target_minutes,
  credit_rate = S.credit_rate,
  notes = S.notes
WHEN NOT MATCHED THEN
  INSERT (tier, restoration_target_minutes, credit_rate, notes)
  VALUES (S.tier, S.restoration_target_minutes, S.credit_rate, S.notes);
