# Fork changes

Upstream baseline: `d6ddd8c` (1.1.7). This local branch prepares Python 3.14
support for the dbt-osmosis fork.

- Extend `requires-python` to include Python 3.14 and identify the fork build as
  `1.1.8+liteart.1`.
- Relax test-only dbt pins so the test environment can resolve dbt Core 1.12
  and a compatible PostgreSQL adapter; refresh `uv.lock`.
- Replace manual source-YAML interpolation with a safe YAML serializer. It
  preserves metadata values as data and combines multiple sources under one
  document. The text layout changes; `quote_identifiers` still quotes values.
- Upgrade SQLFluff to 4.4 to address the vulnerabilities flagged in the prior
  3.5 test environment. The wildcard fixture explicitly excludes the new AM04
  rule while continuing to test the other templater/lint behavior.
- Refresh the server and test dependencies, including FastAPI, Starlette,
  Uvicorn, pytest, coverage and Ruff. The previous lock selected a Starlette
  release with known vulnerabilities. Python 3.10 retains a compatible older
  coverage version.
- Rename internal server model/parameter fields that shadowed Pydantic's
  `schema` method; the public JSON field remains `schema` through aliases.
  Direct Python callers of those server internals should use `schema_name`.
- Correct the server example to bind to localhost and document that the server
  has no built-in authentication and can execute SQL. Remote access requires
  independently configured authentication and network access controls.
- Reject untrusted browser Origins and browser-marked cross-site requests by
  default. Local same-origin and clients without browser headers remain usable;
  remote browser origins require an explicit allowlist. This is not authentication.
- Avoid logging webhook URLs or exception messages in the alert channel because
  these can contain webhook credentials. Failure logs retain the exception type;
  logging configured in third-party HTTP libraries should also be reviewed.
- Expose the naming-enforcer `NamingConvention` as `ModelNamingConvention`,
  preserving the existing top-level `NamingConvention` staging enum. The two
  distinct classes previously collided at the package root.
- Escape quotes, backslashes and line breaks in DOT node labels and identifiers
  to prevent graph metadata from being interpreted as separate DOT statements.
  An independent pydot parser accepted a crafted example without injecting an
  additional edge; actual Graphviz rendering was not tested (binary unavailable).
- Clean up safe lint findings while retaining existing public module imports;
  the remaining security and style findings still require review.
- Fix null-percentage and duplicate-percentage quality SQL, which previously
  counted non-null values or produced invalid SQL. Numeric bounds and simple
  column names are validated, and model references are escaped as Jinja
  literals. Complex/quoted column expressions require an explicit custom SQL
  check. A dbt/DuckDB integration regression checks both percentages against
  real rows through the project, compilation and adapter execution path.
- Local verification with Python 3.14.4 and dbt Core 1.12.6 after the SQL,
  YAML, quality, browser-origin, webhook logging, DOT and lint changes: 346 tests passed,
  two skipped; branch-aware coverage was 54.22% using a Python 3.14 environment installed from the lockfile
  with all extras. `pip-audit` found no known vulnerabilities in the published
  packages in that environment, but could not audit this local fork build.
  A local server heartbeat returned HTTP 200 without startup warnings. Warnings
  remain in upstream tests. This does not satisfy 100% coverage. The
  repository-wide Ruff format check passes. The
  repository-wide Ruff check reports 54 findings; the unchanged upstream
  snapshot reports 131 with the same Ruff version and configuration. It has
  not been made clean.
- Not every transitive package is at its newest release: for example,
  SQLFluff 4.4 requires `click<8.5`, and dbt packages constrain `agate`,
  `mashumaro`, `protobuf` and other dependencies. The lock honors those bounds
  rather than forcing an incompatible environment.

The fork is not yet published or referenced by dbt-osmosis. Further validation
and an accessible GitHub fork are required before relying on it for installs.

## Security review in progress

The source generator now disables Jinja compilation for metadata queries,
validates SQL literal values and unquoted database identifiers before use,
and handles an empty schema exclusion list. This deliberately rejects schema,
table and exclusion names containing apostrophes, backslashes or control
characters; BigQuery and Snowflake identifiers are also restricted to simple
unquoted identifiers. Names outside these constraints require adapter-specific
binding or quoting support; they are rejected rather than guessed at.

Regression tests exercise query-construction branches and actual dbt/DuckDB
metadata queries. PostgreSQL, Redshift, Databricks, Snowflake and BigQuery
were not tested against live warehouses. Bandit also reports dynamic SQL in
other interface modules. The two dynamic SQLite queries in
`performance_profiler.py` construct WHERE clauses from fixed internal fragments
and bind user-provided values as parameters; this inspection does not cover
other modules. Custom SQL quality checks intentionally
accept executable SQL and must only be configured by trusted users.
