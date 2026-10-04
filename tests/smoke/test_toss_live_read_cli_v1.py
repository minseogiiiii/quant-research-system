import pytest

from world_quant_system.toss_live_read.cli import (
    _require_explicit_enable,
    build_parser,
)
from world_quant_system.toss_live_read.models import (
    TossLiveReadError,
    TossLiveReadPolicy,
)


def test_cli_parser_exposes_required_paths() -> None:
    parser = build_parser()
    arguments = parser.parse_args(
        [
            "--policy",
            "policy.json",
            "--output",
            "report.json",
        ]
    )
    assert str(arguments.policy) == "policy.json"
    assert str(arguments.output) == "report.json"


def test_live_cli_requires_exact_opt_in_before_network() -> None:
    with pytest.raises(TossLiveReadError, match="No network request"):
        _require_explicit_enable(TossLiveReadPolicy(), {})
