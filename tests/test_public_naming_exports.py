"""The public API must expose both distinct naming convention types."""

from dbt_core_interface import ModelNamingConvention, NamingConvention
from dbt_core_interface.naming_enforcer import NamingConvention as EnforcerConvention
from dbt_core_interface.staging_generator import NamingConvention as StagingConvention


def test_naming_conventions_remain_distinct_and_backwards_compatible() -> None:
    """Keep the existing staging export and expose the model naming class."""
    assert NamingConvention is StagingConvention
    assert ModelNamingConvention is EnforcerConvention
    assert ModelNamingConvention is not NamingConvention
