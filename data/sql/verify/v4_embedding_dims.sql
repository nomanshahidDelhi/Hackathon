-- v4_embedding_dims.sql
SELECT
  '__LOCATION__' AS expected_location,
  COUNT(*) AS runbooks,
  COUNT(DISTINCT runbook_id) AS distinct_runbooks,
  COUNT(*) - COUNT(DISTINCT runbook_id) AS duplicate_runbooks,
  COUNTIF(ARRAY_LENGTH(embedding) = 768) AS dim_768,
  COUNTIF(embedding IS NULL OR ARRAY_LENGTH(embedding) = 0) AS unembedded,
  MIN(ARRAY_LENGTH(embedding)) AS min_dim,
  MAX(ARRAY_LENGTH(embedding)) AS max_dim,
  COUNTIF((SELECT SUM(ABS(v)) FROM UNNEST(embedding) v) = 0) AS all_zero_vectors
FROM `__PROJECT_ID__.sre_knowledge_base.runbooks`;
