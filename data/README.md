# BCE Hackfest 2026 — Track 2 data kit

The seed SQL for the Track 2 BigQuery warehouse:

    sql/01_schema_additions.sql     datasets + declared-schema tables — run first
    sql/02..07_*.sql                the data, in numeric order (07 reads 05 and 06)
    sql/verify/                     row counts, key integrity, embedding dimensions
    sql/run_all.sh                  fallback loader  
    tools/bq_runner.sh              runs one .sql file and substitutes the placeholders

**Loading this into BigQuery is your task, and it is scored as part of your work.**
Every file carries `__PROJECT_ID__` and `__LOCATION__` placeholders that must be
substituted before execution, `04_generate_embeddings.sql` needs a `vertex_conn`
BigQuery connection with the Vertex AI User role granted to its service agent, and
every step is `CREATE ... IF NOT EXISTS` or `MERGE`, so a re-run repairs a partial
load.

Write your own loader first. 