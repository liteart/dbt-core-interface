"""Business and security regressions for generated quality-check SQL."""

import json
import os
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest
from jinja2 import Environment

from dbt_core_interface import DbtProject
from dbt_core_interface.quality import (
    DuplicateCheck,
    NullPercentageCheck,
    RowCountCheck,
    ValueRangeCheck,
)


def test_null_and_duplicate_percentages_use_all_rows() -> None:
    """Count nulls and duplicates against every row, including unique rows."""
    with duckdb.connect(":memory:") as conn:
        conn.execute("CREATE TABLE quality_data (id INTEGER)")
        conn.execute("INSERT INTO quality_data VALUES (1), (1), (2), (NULL)")

        def render(sql: str) -> str:
            return (
                Environment(autoescape=False)  # noqa: S701 - SQL, not HTML
                .from_string(sql)
                .render(ref=lambda name: "quality_data")
            )

        null_sql = render(NullPercentageCheck(name="nulls", column_name="id").get_sql("model"))
        duplicate_sql = render(DuplicateCheck(name="dups", columns=["id"]).get_sql("model"))
        range_sql = render(
            ValueRangeCheck(name="range", column_name="id", min_value=2).get_sql("model")
        )
        assert conn.execute(null_sql).fetchone()[0] == pytest.approx(25.0)
        assert conn.execute(duplicate_sql).fetchone()[0] == pytest.approx(25.0)
        assert conn.execute(range_sql).fetchone()[0] == 2
        conn.execute("DELETE FROM quality_data")
        assert conn.execute(null_sql).fetchone()[0] is None
        assert conn.execute(duplicate_sql).fetchone()[0] is None
        assert conn.execute(range_sql).fetchone()[0] == 0


@pytest.mark.parametrize(
    "check",
    [
        RowCountCheck(name="rows", min_rows=0),
        NullPercentageCheck(name="nulls", column_name="id"),
        DuplicateCheck(name="dups", columns=["id"]),
        ValueRangeCheck(name="range", column_name="id", min_value=0),
    ],
)
def test_model_name_is_a_jinja_literal(check) -> None:
    """A model name cannot break out of ref() and call arbitrary Jinja."""
    name = "model'); danger(); ref('other"
    seen = []
    Environment(autoescape=False).from_string(check.get_sql(name)).render(  # noqa: S701 - SQL, not HTML
        ref=lambda value: seen.append(value) or "quality_data",
        danger=lambda: pytest.fail("model name executed as Jinja"),
    )
    assert seen == [name]


@pytest.mark.parametrize("name", ["id; DROP TABLE t", "id {{ danger() }}", '"id"', "x.y"])
def test_unsafe_column_names_are_rejected(name: str) -> None:
    """Column identifiers cannot inject SQL or Jinja into generated checks."""
    checks = [
        NullPercentageCheck(name="nulls", column_name=name),
        DuplicateCheck(name="dups", columns=[name]),
        ValueRangeCheck(name="range", column_name=name, min_value=0),
    ]
    for check in checks:
        with pytest.raises(ValueError, match="Unsafe quality-check column"):
            check.get_sql("model")


@pytest.mark.parametrize("value", ["0; DROP TABLE t", float("nan"), float("inf"), True])
def test_unsafe_numeric_bounds_are_rejected(value) -> None:
    """Only finite numeric values may become range-check SQL literals."""
    check = ValueRangeCheck(name="range", column_name="id", min_value=value)
    with pytest.raises(ValueError, match="Unsafe quality-check numeric"):
        check.get_sql("model")


def test_duplicate_check_requires_columns() -> None:
    """An empty grouping cannot generate valid duplicate-check SQL."""
    with pytest.raises(ValueError, match="At least one"):
        DuplicateCheck(name="dups").get_sql("model")


def test_quality_checks_execute_through_dbt_on_duckdb(tmp_path: Path) -> None:
    """Exercise the real dbt compile/adapter path against a disposable database."""
    (tmp_path / "models").mkdir()
    (tmp_path / "dbt_project.yml").write_text(
        "name: quality_smoke\nversion: '1.0'\nconfig-version: 2\n"
        "profile: quality_smoke\nmodel-paths: ['models']\n",
        encoding="utf-8",
    )
    db_path = json.dumps(str(tmp_path / "warehouse.duckdb"))
    (tmp_path / "profiles.yml").write_text(
        "quality_smoke:\n  target: dev\n  outputs:\n    dev:\n"
        f"      type: duckdb\n      path: {db_path}\n      threads: 1\n",
        encoding="utf-8",
    )
    (tmp_path / "models" / "quality_data.sql").write_text(
        "select id from (values (1), (1), (2), (null)) as t(id)\n", encoding="utf-8"
    )
    dbt = Path(sys.executable).with_name("dbt.exe" if os.name == "nt" else "dbt")
    subprocess.run(  # noqa: S603 - fixed dbt executable and isolated fixture paths
        [str(dbt), "run", "--project-dir", str(tmp_path), "--profiles-dir", str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    project = DbtProject(
        project_dir=str(tmp_path), profiles_dir=str(tmp_path), target="dev", autoregister=False
    )
    checks = [
        NullPercentageCheck(name="nulls", column_name="id", max_null_percentage=20),
        DuplicateCheck(name="duplicates", columns=["id"], max_duplicate_percentage=20),
    ]
    for check in checks:
        result = check.execute(project, "quality_data")
        assert result.status.value == "failed"
        assert result.actual_value == pytest.approx(25.0)
