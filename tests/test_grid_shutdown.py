"""Exercise the actual finally blocks without starting trading clients."""
import ast
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock


class ShutdownTest(TestCase):
    def test_initialization_failure_preserves_error_and_closes_resources(self):
        root = Path(__file__).resolve().parents[1] / "example"
        for name, function in (
            ("auto_grid_futures_generic.py", "run_grid"),
            ("auto_grid_contract_futures_generic.py", "run_grid"),
            ("auto_grid_contract_futures_short_generic.py", "run_short_grid"),
        ):
            tree = ast.parse((root / name).read_text(encoding="utf-8"))
            run = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == function)
            final = next(n.finalbody for n in reversed(run.body)
                         if isinstance(n, ast.Try) and n.finalbody)
            for started_count in (0, 1, 2, 3):
                with self.subTest(file=name, started=started_count):
                    stop = threading.Event()
                    workers = [threading.Thread(target=stop.wait) for _ in range(3)]
                    clients = [Mock() for _ in range(4)]
                    store, lock = Mock(), Mock()
                    env = dict(stop=stop, checker=workers[2], listener=workers[1],
                               state_sync=workers[0], clients=clients, store=store,
                               process_lock=lock, main_client=clients[0],
                               checker_client=clients[1], listener_client=clients[2],
                               state_client=clients[3], NETWORK_RETRY_ATTEMPTS=1,
                               NETWORK_TIMEOUT=(1, 1), now_text=lambda: "test")
                    env["core"] = SimpleNamespace(NETWORK_RETRY_ATTEMPTS=1,
                                                   NETWORK_TIMEOUT=(1, 1), now_text=lambda: "test")
                    script = ast.Module(body=[ast.Try(
                        body=ast.parse("raise ValueError('original startup failure')").body,
                        handlers=[], orelse=[], finalbody=final)], type_ignores=[])
                    code = compile(ast.fix_missing_locations(script), name, "exec")
                    try:
                        for worker in workers[:started_count]:
                            worker.start()
                        with self.assertRaisesRegex(ValueError, "original startup failure"):
                            exec(code, env)
                        store.close.assert_called_once()
                        lock.release.assert_called_once()
                        for client in clients:
                            client.close.assert_called_once()
                    finally:
                        stop.set()
                        for worker in workers:
                            if worker.ident is not None:
                                worker.join(timeout=2)
