"""Security boundaries for SQL generated during source discovery."""

from types import SimpleNamespace
from unittest.mock import Mock

import duckdb
import pytest
import yaml

from dbt_core_interface.source_generator import (
    ColumnInfo,
    SourceDefinition,
    SourceGenerationOptions,
    SourceGenerator,
    TableInfo,
    to_yaml,
)


def make_generator(adapter: str = "postgres") -> tuple[SourceGenerator, Mock]:
    """Build a source generator with a recording execution stub."""
    execute = Mock(return_value=SimpleNamespace(table=SimpleNamespace(rows=[])))
    credentials = SimpleNamespace(type=adapter, database="project", schema="public")
    project = SimpleNamespace(
        adapter=Mock(), runtime_config=SimpleNamespace(credentials=credentials), execute_sql=execute
    )
    return SourceGenerator(project), execute


@pytest.mark.parametrize(
    "adapter", ["postgres", "redshift", "databricks", "duckdb", "snowflake", "bigquery"]
)
@pytest.mark.parametrize("name", ["foo' OR 1=1 --", r"foo\' OR 1=1 --", "foo\nbar"])
def test_table_and_column_queries_reject_unsafe_values(adapter: str, name: str) -> None:
    """Reject values that could escape a SQL literal on any supported adapter."""
    generator, _ = make_generator(adapter)
    with pytest.raises(ValueError, match="Unsafe"):
        generator._get_tables_sql(name, "project", adapter)
    with pytest.raises(ValueError, match="Unsafe"):
        generator._get_columns_sql("public", name, adapter)


def test_schema_exclusions_cannot_inject_sql() -> None:
    """Reject unsafe exclusion values before contacting the database."""
    generator, execute = make_generator()
    options = SourceGenerationOptions(exclude_schemas=["public') OR TRUE --"])
    with pytest.raises(ValueError, match="Unsafe"):
        generator._get_postgres_schemas(options)
    execute.assert_not_called()


def test_introspection_does_not_compile_jinja_from_names() -> None:
    """Treat Jinja-like metadata as data rather than executing it."""
    generator, execute = make_generator()
    options = SourceGenerationOptions(exclude_schemas=["{{ danger() }}"])
    assert generator._get_postgres_schemas(options) == []
    assert execute.call_args.kwargs["compile"] is False


def test_database_identifier_cannot_inject_sql() -> None:
    """Reject SQL syntax in unquoted database identifiers."""
    generator, _ = make_generator("snowflake")
    with pytest.raises(ValueError, match="Unsafe"):
        generator._get_tables_sql("public", "db; DROP TABLE x", "snowflake")
    with pytest.raises(ValueError, match="Unsafe"):
        generator._get_tables_sql("public", "db--comment", "snowflake")


def test_bigquery_project_id_can_contain_hyphens() -> None:
    """BigQuery project IDs are quoted and can legitimately contain hyphens."""
    generator, _ = make_generator("bigquery")
    assert "`my-project.public.INFORMATION_SCHEMA.TABLES`" in generator._get_tables_sql(
        "public", "my-project", "bigquery"
    )


@pytest.mark.parametrize(
    "method",
    [
        "_get_postgres_schemas",
        "_get_redshift_schemas",
        "_get_databricks_schemas",
        "_get_duckdb_schemas",
        "_get_bigquery_datasets",
        "_get_snowflake_schemas",
        "_get_generic_schemas",
    ],
)
def test_schema_discovery_handles_empty_exclusions_without_jinja(method: str) -> None:
    """Empty exclusions return all schemas without compiling metadata as Jinja."""
    generator, execute = make_generator()
    assert getattr(generator, method)(SourceGenerationOptions(exclude_schemas=[])) == []
    assert "WHERE 1=1" in execute.call_args.args[0]
    assert execute.call_args.kwargs["compile"] is False


def test_duckdb_metadata_queries_execute_for_normal_names() -> None:
    """The safe SQL remains executable for a real DuckDB metadata query."""
    generator, _ = make_generator("duckdb")
    with duckdb.connect(":memory:") as connection:
        connection.execute("CREATE TABLE orders (id INTEGER)")
        tables = connection.execute(generator._get_tables_sql("main", "", "duckdb")).fetchall()
        columns = connection.execute(
            generator._get_columns_sql("main", "orders", "duckdb")
        ).fetchall()
    assert ("orders",) in tables
    assert columns[0][0] == "id"


@pytest.mark.parametrize("quote_identifiers", [False, True])
def test_source_yaml_keeps_metadata_as_data_and_combines_sources(quote_identifiers: bool) -> None:
    """Quoted text and newlines cannot introduce new dbt YAML properties."""
    description = 'quote "here"\n    config:\n      enabled: false'
    column = ColumnInfo("id", "integer", False, description=description, meta={"n": 1})
    table = TableInfo("orders", "main", "db", columns=[column], description=description)
    source = SourceDefinition("raw", "db", "main", [table], description=description)
    parsed = yaml.safe_load(to_yaml([source, source], quote_identifiers=quote_identifiers))
    assert parsed["version"] == 2
    assert len(parsed["sources"]) == 2
    first = parsed["sources"][0]
    assert first["description"] == description
    assert first["tables"][0]["description"] == description
    assert first["tables"][0]["columns"][0]["description"] == description
    assert first["tables"][0]["columns"][0]["meta"] == {"n": 1}
    assert "config" not in first
    if quote_identifiers:
        assert 'database: "db"' in to_yaml([source], quote_identifiers=True)


def test_empty_source_definitions_preserve_empty_output() -> None:
    """No source definitions continue to produce no output."""
    assert to_yaml([]) == ""
