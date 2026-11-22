"""Read only pure functions from the current scripts; never import or start trading code."""
from __future__ import annotations

import ast
import json
import math
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP
from pathlib import Path
from types import SimpleNamespace

EXAMPLES = Path(__file__).resolve().parents[2] / "example"


def pure_functions(filename, names, core=None):
    tree = ast.parse((EXAMPLES / filename).read_text(encoding="utf-8-sig"))
    definitions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    if {node.name for node in definitions} != set(names):
        raise AssertionError(f"Source functions changed: {filename}")
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, *definitions], type_ignores=[]))
    scope = {"math": math, "Decimal": Decimal, "ROUND_DOWN": ROUND_DOWN, "ROUND_UP": ROUND_UP,
             "ROUND_HALF_UP": ROUND_HALF_UP, "core": core}
    exec(compile(module, filename, "exec"), scope)
    return SimpleNamespace(**scope)


def main():
    core = pure_functions("auto_grid_futures_generic.py", ["round_to_step", "raw_grid_price", "buy_grid_price",
        "first_buy_index", "desired_buy_prices", "take_profit_price", "take_profit_window_upper"])
    short = pure_functions("auto_grid_contract_futures_short_generic.py", ["short_raw_grid_price", "short_grid_price",
        "first_short_index", "desired_short_prices", "short_take_profit_price", "short_take_profit_window_lower"], core)
    futures = pure_functions("auto_grid_contract_futures_generic.py", ["calculate_contract_size"], core)
    cases = []
    for direction in ["long", "short"]:
        for reference, current, step, tick in [("100", "100", ".01", ".01"),
            ("100", "135", ".01", ".01"), ("100", "75", ".01", ".01"),
            ("2385", "2413.7", ".00042", ".01"), ("68920", "68500", ".00042", ".1"),
            ("100", "100", ".0001", ".1")]:
            ref, price, ratio, price_step = map(Decimal, [reference, current, step, tick])
            spec = SimpleNamespace(price_step=price_step, amount_step=Decimal(".001"), min_base_amount=Decimal(".001"),
                                   max_base_amount=Decimal("Infinity"))
            contract_spec = SimpleNamespace(grid_spec=spec, multiplier=Decimal(".001"))
            if direction == "long":
                prices = core.desired_buy_prices(ref, price, ratio, 3, price_step)
                bound = core.take_profit_window_upper(ref, price, ratio, 7, spec)
                tps = [core.take_profit_price(p, Decimal(".013"), spec) for p in prices]
            else:
                prices = short.desired_short_prices(ref, price, ratio, 3, price_step)
                bound = short.short_take_profit_window_lower(ref, price, ratio, 7, spec)
                tps = [short.short_take_profit_price(p, Decimal(".013"), spec) for p in prices]
            sizes = [futures.calculate_contract_size(Decimal("20"), Decimal("3"), p, contract_spec) for p in prices]
            cases.append({"config": {"direction": direction, "referencePrice": float(ref), "gridStepPct": float(ratio),
                "takeProfitPct": .013, "priceStep": float(price_step), "openGridCount": 3,
                "takeProfitGridCount": 7, "quantityStep": .001, "minContracts": .001,
                "marginAmountPerOrder": 20, "leverage": 3, "multiplier": .001},
                "current": float(price), "prices": list(map(float, prices)), "bound": float(bound),
                "takeProfits": list(map(float, tps)), "contracts": list(map(float, sizes))})
    tree = ast.parse((EXAMPLES / "grid_contract_ETH.py").read_text(encoding="utf-8-sig"))
    call = next(node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "run_grid")
    eth = {kw.arg: ast.literal_eval(kw.value) for kw in call.keywords if isinstance(kw.value, ast.Constant)}
    print(json.dumps({"cases": cases, "eth": eth}))


if __name__ == "__main__":
    main()
