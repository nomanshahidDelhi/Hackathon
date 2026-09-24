-- 04_generate_embeddings.sql
CREATE SCHEMA IF NOT EXISTS `__PROJECT_ID__.sre_knowledge_base`
  OPTIONS(location = '__LOCATION__', description = 'SRE runbooks and vector embeddings');

CREATE TABLE IF NOT EXISTS `__PROJECT_ID__.sre_knowledge_base.runbooks`
(
  runbook_id STRING,
  title STRING,
  failure_signature STRING,
  remediation_steps STRING,
  rollback_commands STRING,
  remediation_script STRING,
  embedding ARRAY<FLOAT64>
);

CREATE MODEL IF NOT EXISTS `__PROJECT_ID__.sre_knowledge_base.embedding_model`
  REMOTE WITH CONNECTION `__PROJECT_ID__.__LOCATION__.vertex_conn`
  OPTIONS (endpoint = 'text-embedding-005');

MERGE INTO `__PROJECT_ID__.sre_knowledge_base.runbooks` AS T
USING (
  SELECT
    runbook_id,
    title,
    failure_signature,
    remediation_steps,
    rollback_commands,
    remediation_script,
    embedding
  FROM
    AI.GENERATE_EMBEDDING(
      MODEL `__PROJECT_ID__.sre_knowledge_base.embedding_model`,
      (
        SELECT
          runbook_id,
          title,
          failure_signature,
          remediation_steps,
          rollback_commands,
          remediation_script,
          CONCAT(
            title, '. ',
            'Failure signature: ', failure_signature, '. ',
            'Remediation: ', remediation_steps
          ) AS content
        FROM `__PROJECT_ID__.sre_knowledge_base.runbooks`
      ),
      STRUCT('RETRIEVAL_DOCUMENT' AS task_type, 768 AS output_dimensionality)
    )
) AS S
ON T.runbook_id = S.runbook_id
WHEN MATCHED THEN
  UPDATE SET
    embedding = S.embedding
WHEN NOT MATCHED THEN
  INSERT (runbook_id, title, failure_signature, remediation_steps, rollback_commands, remediation_script, embedding)
  VALUES (S.runbook_id, S.title, S.failure_signature, S.remediation_steps, S.rollback_commands, S.remediation_script, S.embedding);
