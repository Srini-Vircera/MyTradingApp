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


BACKTEST = (
    sorted((SRC / "quant" / "backtest").rglob("*.py"))
    + sorted((SRC / "quant" / "analytics").rglob("*.py"))
    + sorted((SRC / "quant" / "research").rglob("*.py"))
    + sorted((SRC / "quant" / "risk").rglob("*.py"))
    + sorted((SRC / "quant" / "ensemble").rglob("*.py"))
    + sorted((SRC / "quant" / "portfolio").rglob("*.py"))
    + [SRC / "governance" / "research.py", SRC / "governance" / "lifecycle.py"]
)


@pytest.mark.parametrize("path", BACKTEST, ids=lambda p: str(p.relative_to(SRC)))
def test_backtester_cannot_reach_brokers_network_or_credentials(path: Path) -> None:
    forbidden = (
        "httpx",
        "requests",
        "socket",
        "urllib",
        "subprocess",
        "adaptive_quant.config.secrets",
        "adaptive_quant.quant.data.providers",
        "adaptive_quant.trading",
    )
    bad = {m for m in imports(path) if m in forbidden or m.startswith(forbidden)}
    assert not bad, f"{path.name} imports {bad}"
    text = path.read_text()
    for token in ("BrokerAdapter", "submit_order", "getenv", "environ", "SecretStr"):
        assert token not in text, f"{path.name} references {token}"


def test_only_the_order_manager_transmits_orders() -> None:
    """``submit_order(`` may be called only by the order manager (and defined by adapters)."""
    callers = {
        str(p.relative_to(SRC)) for p in SRC.rglob("*.py") if ".submit_order(" in p.read_text()
    }
    assert callers == {"trading/orders/manager.py"}


API = [*sorted((SRC / "api").rglob("*.py")), SRC / "cli_api.py"]


@pytest.mark.parametrize("path", API, ids=lambda p: str(p.relative_to(SRC)))
def test_api_cannot_reach_brokers_orders_or_the_trading_cycle(path: Path) -> None:
    forbidden = (
        "adaptive_quant.trading.brokers",
        "adaptive_quant.trading.orders",
        "adaptive_quant.trading.scheduler.cycle",
        "adaptive_quant.trading.scheduler.runner",
        "adaptive_quant.config.secrets",
    )
    allowed_secrets = {
        "cli_api.py"
    }  # the CLI reads AQ_API_TOKEN / DATABASE_URL; the app never does
    bad = {
        m
        for m in imports(path)
        if m.startswith(forbidden)
        and not (path.name in allowed_secrets and m == "adaptive_quant.config.secrets")
    }
    assert not bad, f"{path.name} imports {bad}"
    text = path.read_text()
    for token in ("submit_order", "cancel_order", "trading.mode =", 'model_copy(update={"trading"'):
        assert token not in text, f"{path.name} references {token}"
