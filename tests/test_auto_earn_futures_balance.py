"""自动资金管理的离线测试。"""
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from example import auto_earn_futures_balance as auto


class BalanceTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.client = Mock()
        self.futures = Mock()
        self.earn = Mock()
        self.earn.list_user_uni_lends.return_value = [
            SimpleNamespace(currency="USDT", current_amount="50", amount="50", frozen_amount="0")
        ]
        self.steps = Mock()
        for item in (patch.object(auto, "FuturesApi", return_value=self.futures),
                     patch.object(auto, "EarnUniApi", return_value=self.earn),
                     patch.object(auto.flow, "run_steps", self.steps),
                     patch.object(auto, "log")):
            item.start()
            self.addCleanup(item.stop)

    def check(self, balance, *, execute=True, mode=0):
        self.futures.list_futures_accounts.return_value = SimpleNamespace(
            available=balance, total="9999", margin_mode=mode,
        )
        auto.check_once(self.client, self.directory, execute)

    def write_progress(self, direction, stage, **values):
        path = self.directory / f"{direction}.json"
        path.write_text(json.dumps(dict(stage=stage, amount="50", **values)), encoding="utf-8")
        return path

    def test_only_strict_thresholds_trigger_and_use_available_not_total(self):
        for balance in ("100", "150", "200"):
            with self.subTest(balance=balance):
                self.check(balance)
                self.steps.assert_not_called()
        for balance, direction in (("200.01", auto.TO_EARN), ("99.99", auto.TO_FUTURES)):
            with self.subTest(balance=balance):
                self.check(balance)
                self.assertEqual((direction, Decimal("50")), self.steps.call_args.args[1:3])

    def test_pending_redemption_resumes_instead_of_starting_opposite_transfer(self):
        path = self.write_progress(auto.TO_FUTURES, "redeem_wait")
        self.check("201")
        self.steps.assert_called_once_with(
            self.client, auto.TO_FUTURES, Decimal("50"), None, path,
            wait_timeout=0, poll_interval=auto.INTERVAL_SECONDS,
        )

    def test_unconfirmed_submission_is_never_reset_by_monitor(self):
        path = self.write_progress(auto.TO_EARN, "transfer_submitting")
        self.check("90")
        self.steps.assert_called_once_with(
            self.client, auto.TO_EARN, Decimal("50"), None, path,
            wait_timeout=0, poll_interval=auto.INTERVAL_SECONDS,
        )

    def test_completed_task_can_start_one_new_transfer(self):
        self.write_progress(auto.TO_EARN, "done")
        self.check("220")
        self.steps.assert_called_once()
        self.assertTrue(self.steps.call_args.kwargs["reset"])

    def test_unsubmitted_outgoing_plan_does_not_block_low_balance_top_up(self):
        outgoing = self.write_progress(auto.TO_EARN, "transfer_ready")
        self.check("90")
        self.assertFalse(outgoing.exists())
        self.assertEqual(1, len(list(self.directory.glob("futures_to_earn.*.bak.json"))))
        self.steps.assert_called_once()
        self.assertEqual(auto.TO_FUTURES, self.steps.call_args.args[1])

    def test_low_balance_skips_if_earn_has_insufficient_or_pending_funds(self):
        row = self.earn.list_user_uni_lends.return_value[0]
        for amount, frozen in (("49.99", "0"), ("100", "1")):
            with self.subTest(amount=amount, frozen=frozen):
                row.current_amount, row.frozen_amount = amount, frozen
                self.check("99")
                self.steps.assert_not_called()

    def test_low_balance_accepts_gate_record_with_null_current_amount(self):
        row = self.earn.list_user_uni_lends.return_value[0]
        row.current_amount = None
        row.amount = "470.495207"
        self.check("84")
        self.steps.assert_called_once()
        self.assertEqual((auto.TO_FUTURES, Decimal("50")), self.steps.call_args.args[1:3])

    def test_explicit_zero_current_amount_is_not_replaced_by_total(self):
        row = self.earn.list_user_uni_lends.return_value[0]
        row.current_amount = "0"
        row.amount = "470.495207"
        self.check("84")
        self.steps.assert_not_called()

    def test_preview_and_unsupported_account_do_not_move_funds(self):
        self.check("201", execute=False)
        self.check("99", execute=False)
        with self.assertRaisesRegex(RuntimeError, "经典"):
            self.check("201", mode=1)
        self.steps.assert_not_called()
        self.assertEqual([], list(self.directory.iterdir()))

    def test_main_has_no_cli_and_uses_embedded_execute_switch(self):
        configuration = SimpleNamespace(host="https://example.test/api/v4", key="demo-key")
        with patch.dict("os.environ", {"GATE_API_KEY": "demo-key", "GATE_API_SECRET": "demo-secret"}, clear=True), \
                patch.object(auto.flow, "load_env_file"), \
                patch.object(auto.flow, "make_configuration", return_value=configuration), \
                patch.object(auto.flow, "transfer_account_id", return_value="demo"), \
                patch.object(auto.flow, "transfer_account_lock") as transfer_lock, \
                patch.object(auto.flow, "STATE_DIR", self.directory), \
                patch.object(auto, "ApiClient") as api_client, \
                patch.object(auto, "monitor", return_value=0) as monitor:
            for execute in (False, True):
                with self.subTest(execute=execute), patch.object(auto, "EXECUTE", execute):
                    transfer_lock.reset_mock()
                    self.assertEqual(0, auto.main())
                    self.assertEqual(execute, monitor.call_args.kwargs["execute"])
                    self.assertFalse(monitor.call_args.kwargs["once"])
                    self.assertIs(monitor.call_args.args[0], api_client.return_value.__enter__.return_value)
                    if execute:
                        transfer_lock.return_value.acquire.assert_called_once()
                        transfer_lock.return_value.release.assert_called_once()
                    else:
                        transfer_lock.assert_not_called()

    def test_monitor_waits_remaining_interval_even_after_query_error(self):
        with patch.object(auto, "check_once", side_effect=TimeoutError("network")), \
                patch.object(auto.time, "monotonic", side_effect=[10, 12]), \
                patch.object(auto.time, "sleep", side_effect=KeyboardInterrupt) as sleep:
            with self.assertRaises(KeyboardInterrupt):
                auto.monitor(self.client, self.directory, execute=True, once=False)
        sleep.assert_called_once_with(auto.INTERVAL_SECONDS - 2)


if __name__ == "__main__":
    unittest.main()
