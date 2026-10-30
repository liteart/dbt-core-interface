#!/usr/bin/env python
# pyright: reportAny=false,reportUnknownMemberType=false,reportUnknownVariableType=false
"""Source configuration generator for dbt-core-interface.

This module provides functionality to automatically generate dbt source YAML
configurations by introspecting the database schema. It creates source definitions
with proper column names, descriptions, and data types.
"""

from __future__ import annotations

import logging
import re
import typing as t
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path  # noqa: F401 - retained for compatibility

import yaml

from dbt_core_interface.project import DbtProject

logger = logging.getLogger(__name__)


def _safe_sql_literal_value(value: str) -> str:
    """Reject characters that can escape literals across supported SQL dialects."""
    if not isinstance(value, str) or any(
        char in "'\\" or ord(char) < 32 or ord(char) == 127 for char in value
    ):
        raise ValueError(f"Unsafe value for source introspection: {value!r}")
    return value


def _safe_sql_identifier(value: str, *, allow_hyphen: bool = False) -> str:
    """Allow only simple unquoted identifiers used in metadata queries."""
    pattern = r"[A-Za-z_][A-Za-z0-9_$-]*" if allow_hyphen else r"[A-Za-z_][A-Za-z0-9_$]*"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError(f"Unsafe identifier for source introspection: {value!r}")
    return value


def _schema_filter(options: SourceGenerationOptions) -> str:
    if not options.exclude_schemas:
        return "1=1"
    values = ",".join(f"'{_safe_sql_literal_value(name)}'" for name in options.exclude_schemas)
    return f"schema_name NOT IN ({values})"


class SourceGenerationStrategy(str, Enum):
    """Strategies for source generation."""

    ALL_SCHEMAS = "all_schemas"
    SPECIFIC_SCHEMA = "specific_schema"
    SPECIFIC_TABLES = "specific_tables"


@dataclass(frozen=True)
class ColumnInfo:
    """Information about a database column."""

    name: str
    data_type: str
    is_nullable: bool
    description: str = ""
    tags: list[str] = field(default_factory=list)
    meta: dict[str, t.Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TableInfo:
    """Information about a database table."""

    name: str
    schema: str
    database: str
    columns: list[ColumnInfo] = field(default_factory=list)
    description: str = ""
    tags: list[str] = field(default_factory=list)
    meta: dict[str, t.Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceDefinition:
    """Generated source definition."""

    source_name: str
    database: str
    schema: str
    tables: list[TableInfo]
    description: str = ""
    tags: list[str] = field(default_factory=list)
    meta: dict[str, t.Any] = field(default_factory=dict)


@dataclass
class SourceGenerationOptions:
    """Options for source generation."""

    strategy: SourceGenerationStrategy = SourceGenerationStrategy.SPECIFIC_SCHEMA
    schema_name: str | None = None
    table_names: list[str] = field(default_factory=list)
    source_name: str = "raw"
    include_descriptions: bool = True
    infer_descriptions: bool = True
    exclude_schemas: list[str] = field(default_factory=lambda: ["information_schema", "pg_catalog"])
    exclude_tables: list[str] = field(default_factory=list)
    include_tags: bool = False
    quote_identifiers: bool = False


class SourceGenerator:
    """Generate dbt source YAML configurations from database introspection."""

    def __init__(self, project: DbtProject) -> None:
        """Initialize the source generator.

        Args:
            project: The DbtProject instance to use for database access.

        """
        self.project = project
        self.adapter = project.adapter
        self.credentials = project.runtime_config.credentials

    def generate_sources(
        self, options: SourceGenerationOptions | None = None
    ) -> list[SourceDefinition]:
        """Generate source definitions from the database.

        Args:
            options: Configuration options for generation.

        Returns:
            List of generated SourceDefinition objects.

        """
        options = options or SourceGenerationOptions()

        if options.strategy == SourceGenerationStrategy.SPECIFIC_SCHEMA:
            if not options.schema_name:
                raise ValueError("schema_name is required for SPECIFIC_SCHEMA strategy")
            return self._generate_for_schema(options.schema_name, options)
        elif options.strategy == SourceGenerationStrategy.SPECIFIC_TABLES:
            if not options.table_names:
                raise ValueError("table_names is required for SPECIFIC_TABLES strategy")
            return self._generate_for_tables(options.table_names, options)
        else:  # ALL_SCHEMAS
            return self._generate_for_all_schemas(options)

    def _generate_for_schema(
        self, schema: str, options: SourceGenerationOptions
    ) -> list[SourceDefinition]:
        """Generate sources for a specific schema."""
        tables = self._get_tables_for_schema(schema, options)
        if not tables:
            logger.warning(f"No tables found in schema '{schema}'")
            return []

        source_def = SourceDefinition(
            source_name=options.source_name,
            database=self.credentials.database or self.credentials.schema,
            schema=schema,
            tables=tables,
        )
        return [source_def]

    def _generate_for_tables(
        self, table_names: list[str], options: SourceGenerationOptions
    ) -> list[SourceDefinition]:
        """Generate sources for specific tables."""
        if not options.schema_name:
            # Try to infer schema from table names or use default
            schema = self.credentials.schema or "public"
        else:
            schema = options.schema_name

        tables = []
        for table_name in table_names:
            table_info = self._introspect_table(
                self.credentials.database or "",
                schema,
                table_name,
                options,
            )
            if table_info:
                tables.append(table_info)

        if not tables:
            logger.warning(f"No tables found from specified list: {table_names}")
            return []

        source_def = SourceDefinition(
            source_name=options.source_name,
            database=self.credentials.database or "",
            schema=schema,
            tables=tables,
        )
        return [source_def]

    def _generate_for_all_schemas(self, options: SourceGenerationOptions) -> list[SourceDefinition]:
        """Generate sources for all accessible schemas."""
        schemas = self._get_schemas(options)

        sources = []
        for schema in schemas:
            tables = self._get_tables_for_schema(schema, options)
            if tables:
                source_def = SourceDefinition(
                    source_name=options.source_name,
                    database=self.credentials.database or "",
                    schema=schema,
                    tables=tables,
                )
                sources.append(source_def)

        return sources

    def _get_schemas(self, options: SourceGenerationOptions) -> list[str]:
        """Get list of accessible schemas."""
        adapter_type = self.credentials.type

        if adapter_type == "postgres":
            return self._get_postgres_schemas(options)
        elif adapter_type == "bigquery":
            return self._get_bigquery_datasets(options)
        elif adapter_type == "snowflake":
            return self._get_snowflake_schemas(options)
        elif adapter_type == "redshift":
            return self._get_redshift_schemas(options)
        elif adapter_type == "databricks":
            return self._get_databricks_schemas(options)
        elif adapter_type == "duckdb":
            return self._get_duckdb_schemas(options)
        else:
            # Generic fallback
            return self._get_generic_schemas(options)

    def _get_postgres_schemas(self, options: SourceGenerationOptions) -> list[str]:
        """Get schemas for PostgreSQL."""
        sql = """
            SELECT schema_name
            FROM information_schema.schemata
            WHERE %s
            ORDER BY schema_name
        """
        sql = sql % _schema_filter(options)

        result = self.project.execute_sql(sql, compile=False)
        schemas = [str(row[0]) for row in result.table.rows]
        return schemas

    def _get_bigquery_datasets(self, options: SourceGenerationOptions) -> list[str]:
        """Get datasets for BigQuery."""
        project_id = _safe_sql_identifier(
            self.credentials.database or self.credentials.schema, allow_hyphen=True
        )
        sql = f"""
            SELECT schema_name
            FROM `{project_id}.INFORMATION_SCHEMA.SCHEMATA`
            WHERE {_schema_filter(options)}
            ORDER BY schema_name
        """
        result = self.project.execute_sql(sql, compile=False)
        schemas = [str(row[0]) for row in result.table.rows]
        return schemas

    def _get_snowflake_schemas(self, options: SourceGenerationOptions) -> list[str]:
        """Get schemas for Snowflake."""
        database = _safe_sql_identifier(self.credentials.database)
        sql = f"""
            SELECT schema_name
            FROM {database}.INFORMATION_SCHEMA.SCHEMATA
            WHERE {_schema_filter(options)}
            ORDER BY schema_name
        """
        result = self.project.execute_sql(sql, compile=False)
        schemas = [str(row[0]) for row in result.table.rows]
        return schemas

    def _get_redshift_schemas(self, options: SourceGenerationOptions) -> list[str]:
        """Get schemas for Redshift (similar to PostgreSQL)."""
        return self._get_postgres_schemas(options)

    def _get_databricks_schemas(self, options: SourceGenerationOptions) -> list[str]:
        """Get schemas for Databricks."""
        sql = """
            SELECT schema_name
            FROM information_schema.schemata
            WHERE %s
            ORDER BY schema_name
        """
        sql = sql % _schema_filter(options)
        result = self.project.execute_sql(sql, compile=False)
        schemas = [str(row[0]) for row in result.table.rows]
        return schemas

    def _get_duckdb_schemas(self, options: SourceGenerationOptions) -> list[str]:
        """Get schemas for DuckDB."""
        sql = """
            SELECT schema_name
            FROM information_schema.schemata
            WHERE %s
            ORDER BY schema_name
        """
        sql = sql % _schema_filter(options)
        result = self.project.execute_sql(sql, compile=False)
        schemas = [str(row[0]) for row in result.table.rows]
        return schemas

    def _get_generic_schemas(self, options: SourceGenerationOptions) -> list[str]:
        """Generic schema discovery (SQL standard)."""
        sql = """
            SELECT schema_name
            FROM information_schema.schemata
            WHERE %s
            ORDER BY schema_name
        """
        sql = sql % _schema_filter(options)
        try:
            result = self.project.execute_sql(sql, compile=False)
            return [str(row[0]) for row in result.table.rows]
        except Exception:
            # Fallback: just use the configured schema
            return [self.credentials.schema or "public"]

    def _get_tables_for_schema(
        self, schema: str, options: SourceGenerationOptions
    ) -> list[TableInfo]:
        """Get all tables in a schema."""
        adapter_type = self.credentials.type
        database = self.credentials.database or ""

        # Get table names
        tables_sql = self._get_tables_sql(schema, database, adapter_type)
        result = self.project.execute_sql(tables_sql, compile=False)
        table_names = [str(row[0]) for row in result.table.rows]

        # Filter excluded tables
        table_names = [
            t for t in table_names if t not in options.exclude_tables and not t.startswith("_")
        ]

        tables = []
        for table_name in table_names:
            table_info = self._introspect_table(database, schema, table_name, options)
            if table_info:
                tables.append(table_info)

        return tables

    def _get_tables_sql(self, schema: str, database: str, adapter_type: str) -> str:
        """Get SQL to list tables in a schema."""
        schema = _safe_sql_literal_value(schema)
        if adapter_type in {"bigquery", "snowflake"}:
            database = _safe_sql_identifier(database, allow_hyphen=adapter_type == "bigquery")
        if adapter_type == "bigquery":
            schema = _safe_sql_identifier(schema, allow_hyphen=True)
        if adapter_type == "postgres":
            return f"""
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = '{schema}'
                AND table_type = 'BASE TABLE'
                ORDER BY table_name
            """
        elif adapter_type == "bigquery":
            return f"""
                SELECT table_name
                FROM `{database}.{schema}.INFORMATION_SCHEMA.TABLES`
                WHERE table_type = 'BASE TABLE'
                ORDER BY table_name
            """
        elif adapter_type == "snowflake":
            return f"""
                SELECT table_name
                FROM {database}.INFORMATION_SCHEMA.TABLES
                WHERE table_schema = '{schema}'
                AND table_type = 'BASE TABLE'
                ORDER BY table_name
            """
        elif adapter_type == "redshift":
            return f"""
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = '{schema}'
                AND table_type = 'BASE TABLE'
                ORDER BY table_name
            """
        elif adapter_type == "databricks":
            return f"""
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = '{schema}'
                AND table_type = 'BASE TABLE'
                ORDER BY table_name
            """
        elif adapter_type == "duckdb":
            return f"""
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = '{schema}'
                AND table_type = 'BASE TABLE'
                ORDER BY table_name
            """
        else:
            # Generic SQL standard
            return f"""
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = '{schema}'
                AND table_type = 'BASE TABLE'
                ORDER BY table_name
            """

    def _introspect_table(
        self,
        database: str,
        schema: str,
        table_name: str,
        options: SourceGenerationOptions,
    ) -> TableInfo | None:
        """Introspect a table to get its column information."""
        try:
            columns = self._get_columns(database, schema, table_name, options)
            if not columns:
                logger.warning(f"No columns found for {schema}.{table_name}")
                return None

            return TableInfo(
                name=table_name,
                schema=schema,
                database=database,
                columns=columns,
                description=self._infer_table_description(table_name, columns)
                if options.infer_descriptions
                else "",
            )
        except Exception as e:
            logger.error(f"Error introspecting {schema}.{table_name}: {e}")
            return None

    def _get_columns(
        self,
        database: str,
        schema: str,
        table_name: str,
        options: SourceGenerationOptions,
    ) -> list[ColumnInfo]:
        """Get column information for a table."""
        adapter_type = self.credentials.type
        columns_sql = self._get_columns_sql(schema, table_name, adapter_type)

        result = self.project.execute_sql(columns_sql, compile=False)
        columns = []

        for row in result.table.rows:
            col_name = str(row[0])
            data_type = str(row[1])
            is_nullable = str(row[2]).upper() == "YES" if len(row) > 2 else True

            description = ""
            if options.infer_descriptions:
                description = self._infer_column_description(col_name, data_type)

            tags = []
            if options.include_tags:
                tags = self._infer_column_tags(col_name, data_type, is_nullable)

            columns.append(
                ColumnInfo(
                    name=col_name,
                    data_type=data_type,
                    is_nullable=is_nullable,
                    description=description,
                    tags=tags,
                )
            )

        return columns

    def _get_columns_sql(self, schema: str, table_name: str, adapter_type: str) -> str:
        """Get SQL to list columns in a table."""
        schema = _safe_sql_literal_value(schema)
        table_name = _safe_sql_literal_value(table_name)
        if adapter_type == "bigquery":
            database = _safe_sql_identifier(self.credentials.database or schema, allow_hyphen=True)
            schema = _safe_sql_identifier(schema, allow_hyphen=True)
            return f"""
                SELECT
                    column_name,
                    data_type,
                    is_nullable
                FROM `{database}.{schema}.INFORMATION_SCHEMA.COLUMNS`
                WHERE table_name = '{table_name}'
                ORDER BY ordinal_position
            """
        elif adapter_type == "snowflake":
            database = _safe_sql_identifier(self.credentials.database or "")
            return f"""
                SELECT
                    column_name,
                    data_type,
                    is_nullable
                FROM {database}.INFORMATION_SCHEMA.COLUMNS
                WHERE table_schema = '{schema}'
                AND table_name = '{table_name}'
                ORDER BY ordinal_position
            """
        else:
            # Standard SQL (works for postgres, redshift, databricks, duckdb)
            return f"""
                SELECT
                    column_name,
                    data_type,
                    is_nullable
                FROM information_schema.columns
                WHERE table_schema = '{schema}'
                AND table_name = '{table_name}'
                ORDER BY ordinal_position
            """

    def _infer_table_description(self, table_name: str, columns: list[ColumnInfo]) -> str:
        """Infer a description for a table."""
        # Remove common prefixes/suffixes
        clean_name = table_name
        for prefix in ["tbl_", "t_", "dim_", "fct_", "stg_", "raw_"]:
            if clean_name.startswith(prefix):
                clean_name = clean_name[len(prefix) :]
                break

        # Convert snake_case to Title Case
        clean_name = clean_name.replace("_", " ").title()

        return f"Source table: {clean_name}"

    def _infer_column_description(self, column_name: str, data_type: str) -> str:
        """Infer a description for a column."""
        clean_name = column_name.replace("_", " ").strip().title()

        # Add type information for certain data types
        type_suffix = ""
        if "timestamp" in data_type.lower() or "date" in data_type.lower():
            type_suffix = " (timestamp)"
        elif "bool" in data_type.lower():
            type_suffix = " (boolean)"

        return f"{clean_name}{type_suffix}"

    def _infer_column_tags(self, column_name: str, data_type: str, is_nullable: bool) -> list[str]:
        """Infer tags for a column based on its characteristics."""
        tags = []

        if not is_nullable:
            tags.append("not_null")

        if column_name == "id" or column_name.endswith("_id"):
            tags.append("primary_key" if column_name == "id" else "foreign_key")

        if (
            column_name.endswith("_at")
            or column_name.endswith("_date")
            or column_name.endswith("_time")
        ):
            tags.append("timestamp")

        if (
            column_name.startswith("is_")
            or column_name.startswith("has_")
            or column_name.startswith("can_")
        ):
            tags.append("boolean")

        return tags


def to_yaml(source_defs: list[SourceDefinition], quote_identifiers: bool = False) -> str:
    """Convert source definitions to dbt YAML format.

    Args:
        source_defs: List of source definitions to convert.
        quote_identifiers: Whether to quote database/table/column identifiers.

    Returns:
        YAML string representation of the sources.

    """
    if not source_defs:
        return ""

    class QuotedIdentifier(str):
        pass

    class SourceDumper(yaml.SafeDumper):
        pass

    SourceDumper.add_representer(
        QuotedIdentifier,
        lambda dumper, value: dumper.represent_scalar("tag:yaml.org,2002:str", value, style='"'),
    )

    def identifier(value: str) -> str:
        return QuotedIdentifier(value) if quote_identifiers else value

    def common_fields(item: t.Any) -> dict[str, t.Any]:
        fields: dict[str, t.Any] = {}
        if item.description:
            fields["description"] = item.description
        if item.tags:
            fields["tags"] = item.tags
        if item.meta:
            fields["meta"] = item.meta
        return fields

    sources: list[dict[str, t.Any]] = []
    for source_def in source_defs:
        source: dict[str, t.Any] = {
            "name": source_def.source_name,
            "database": identifier(source_def.database),
            "schema": identifier(source_def.schema),
            **common_fields(source_def),
            "tables": [],
        }
        for table in source_def.tables:
            table_data: dict[str, t.Any] = {
                "name": identifier(table.name),
                **common_fields(table),
                "columns": [],
            }
            for column in table.columns:
                table_data["columns"].append(
                    {
                        "name": identifier(column.name),
                        "data_type": column.data_type,
                        **common_fields(column),
                    }
                )
            source["tables"].append(table_data)
        sources.append(source)
    return yaml.dump(
        {"version": 2, "sources": sources},
        Dumper=SourceDumper,
        sort_keys=False,
        allow_unicode=True,
    )


__all__ = [
    "SourceGenerator",
    "SourceDefinition",
    "TableInfo",
    "ColumnInfo",
    "SourceGenerationOptions",
    "SourceGenerationStrategy",
    "to_yaml",
]
