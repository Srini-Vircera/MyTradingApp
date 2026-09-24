"""Static guarantees: research code cannot reach brokers, the network or credentials."""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[3] / "src" / "adaptive_quant"
QUANT = sorted((SRC / "quant").rglob("*.py"))
STRATEGIES = sorted((SRC / "quant" / "strategies").rglob("*.py"))
INDICATORS = sorted((SRC / "quant" / "indicators").rglob("*.py"))


def imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


@pytest.mark.parametrize("path", QUANT, ids=lambda p: str(p.relative_to(SRC)))
def test_quant_never_imports_trading(path: Path) -> None:
    bad = {m for m in imports(path) if m.startswith("adaptive_quant.trading")}
    assert not bad, f"{path.name} imports execution code: {bad}"


@pytest.mark.parametrize("path", STRATEGIES + INDICATORS, ids=lambda p: str(p.relative_to(SRC)))
def test_strategies_and_indicators_have_no_io_or_secrets(path: Path) -> None:
    forbidden = (
        "httpx",
        "requests",
        "socket",
        "urllib",
        "os",
        "subprocess",
        "adaptive_quant.config.secrets",
        "adaptive_quant.quant.data.providers",
        "adaptive_quant.quant.data.store",
    )
    bad = {m for m in imports(path) if m in forbidden or m.startswith(forbidden)}
    assert not bad, f"{path.name} imports {bad}"
    text = path.read_text()
    for token in ("submit_order", "environ", "getenv", "SecretStr", "BrokerAdapter"):
        assert token not in text, f"{path.name} references {token}"
