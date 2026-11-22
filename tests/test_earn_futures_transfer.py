"""资金示例的离线测试，API 全部替换为模拟对象。"""
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as NS
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from example import earn_futures_transfer_common as flow
from gate_api.exceptions import ApiException, GateApiException


class TransferTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.json"
        self.client = Mock(configuration=NS(host="https://example.test/api/v4", key="dummy-test-key"))
        self.earn, self.wallet, self.futures = Mock(), Mock(), Mock()
        self.futures.list_futures_accounts.return_value = NS(margin_mode=0, available="100")
        self.earn.get_uni_currency.return_value = NS(min_lend_amount="1", min_rate="0.000001", max_rate="0.001")
        self.wallet.transfer.return_value = NS(tx_id=123)
        self.wallet.get_transfer.side_effect = lambda *a, **k: self.detail("success")
        self.patches = [patch.object(flow, "EarnUniApi", return_value=self.earn),
                        patch.object(flow, "WalletApi", return_value=self.wallet),
                        patch.object(flow, "FuturesApi", return_value=self.futures),
                        patch("builtins.print")]
        for item in self.patches:
            item.start()
        self.client.call_api.return_value = [{"currency": "USDT", "available": "0"}]

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def run_flow(self, direction="earn_to_futures", **kwargs):
        return flow.run_steps(self.client, direction, Decimal("10"), None, self.path,
                              wait_timeout=0, poll_interval=2, **kwargs)

    def detail(self, status):
        body = self.wallet.transfer.call_args.args[0]
        return NS(tx_id="123", status=status, currency="USDT", amount="10",
                  from_account=body._from, to_account=body.to, settle="usdt")

    def state(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def balances(self):
        self.client.call_api.side_effect = [[{"currency": "USDT", "available": n}]
                                            for n in ("5", "15", "15")]

    def test_redeem_then_confirm_balance_then_transfer(self):
        self.balances()
        self.run_flow()
        request = self.earn.create_uni_lend.call_args.args[0]
        self.assertEqual(("USDT", "redeem", "10"), (request.currency, request.type, request.amount))
        request = self.wallet.transfer.call_args.args[0]
        self.assertEqual(("USDT", "spot", "futures", "usdt", "10"),
                         (request.currency, request._from, request.to, request.settle, request.amount))
        self.assertEqual("done", self.state()["stage"])
        self.wallet.get_transfer.assert_called_once_with("123", _request_timeout=flow.TIMEOUT)
        self.run_flow()
        self.wallet.transfer.assert_called_once()
        self.earn.create_uni_lend.assert_called_once()

    def test_reverse_confirms_transfer_before_lending_with_current_minimum_rate(self):
        self.balances()
        def lend(body, **kwargs):
            self.wallet.get_transfer.assert_called_once()
            self.assertEqual(("lend", "10", "0.000001"), (body.type, body.amount, body.min_rate))
        self.earn.create_uni_lend.side_effect = lend
        self.run_flow("futures_to_earn")
        request = self.wallet.transfer.call_args.args[0]
        self.assertEqual(("futures", "spot"), (request._from, request.to))
        self.assertEqual("done", self.state()["stage"])

    def test_redemption_wait_timeout_is_resumable_without_second_redemption(self):
        with self.assertRaises(TimeoutError):
            self.run_flow()
        self.assertEqual("redeem_wait", self.state()["stage"])
        self.wallet.transfer.assert_not_called()
        self.client.call_api.return_value = [{"currency": "USDT", "available": "10"}]
        self.run_flow()
        self.earn.create_uni_lend.assert_called_once()
        self.assertEqual("done", self.state()["stage"])

    def test_ambiguous_redemption_never_repeats_post(self):
        self.earn.create_uni_lend.side_effect = TimeoutError("unknown outcome")
        with self.assertRaises(TimeoutError):
            self.run_flow()
        with self.assertRaisesRegex(RuntimeError, "需要核对"):
            self.run_flow()
        self.earn.create_uni_lend.assert_called_once()
        self.wallet.transfer.assert_not_called()

    def test_ambiguous_transfer_never_repeats_post(self):
        self.wallet.transfer.side_effect = TimeoutError("unknown outcome")
        with self.assertRaises(TimeoutError):
            self.run_flow("futures_to_earn")
        with self.assertRaisesRegex(RuntimeError, "需要核对"):
            self.run_flow("futures_to_earn")
        self.wallet.transfer.assert_called_once()
        self.earn.create_uni_lend.assert_not_called()

    def test_wallet_permission_rejection_leaves_transfer_ready(self):
        self.balances()
        self.wallet.transfer.side_effect = GateApiException(
            "FORBIDDEN", "Request API key does not have wallet permission",
            exp=ApiException(status=403),
        )
        with self.assertRaisesRegex(RuntimeError, "缺少 Wallet"):
            self.run_flow()
        self.assertEqual("transfer_ready", self.state()["stage"])
        self.earn.create_uni_lend.assert_called_once()

    def test_explicit_retry_of_legacy_state_does_not_redeem_again(self):
        self.balances()
        self.wallet.transfer.side_effect = TimeoutError("legacy checkpoint")
        with self.assertRaises(TimeoutError):
            self.run_flow()
        self.wallet.transfer.side_effect = None
        self.client.call_api.side_effect = None
        self.client.call_api.return_value = [{"currency": "USDT", "available": "15"}]
        self.run_flow(retry_rejected_transfer=True)
        self.earn.create_uni_lend.assert_called_once()
        self.assertEqual("done", self.state()["stage"])

    def test_explicit_retry_cannot_create_a_new_task(self):
        with self.assertRaisesRegex(RuntimeError, "没有原任务"):
            self.run_flow(retry_rejected_transfer=True)
        self.wallet.transfer.assert_not_called()
        self.earn.create_uni_lend.assert_not_called()

    def test_ambiguous_lending_never_repeats_post(self):
        self.balances()
        self.earn.create_uni_lend.side_effect = TimeoutError("unknown outcome")
        with self.assertRaises(TimeoutError):
            self.run_flow("futures_to_earn")
        with self.assertRaisesRegex(RuntimeError, "需要核对"):
            self.run_flow("futures_to_earn")
        self.wallet.transfer.assert_called_once()
        self.earn.create_uni_lend.assert_called_once()

    def test_retries_legacy_rate_as_plain_decimal_without_another_transfer(self):
        self.balances()
        self.earn.get_uni_currency.return_value.min_rate = "0.00000011"
        self.earn.create_uni_lend.side_effect = TimeoutError("legacy checkpoint")
        with self.assertRaises(TimeoutError):
            self.run_flow("futures_to_earn")
        self.assertEqual("0.00000011", self.earn.create_uni_lend.call_args.args[0].min_rate)
        state = self.state()
        state["min_rate"] = "1.1E-7"
        self.path.write_text(json.dumps(state), encoding="utf-8")
        self.earn.create_uni_lend.side_effect = None
        self.run_flow("futures_to_earn", retry_rejected_lend=True)
        self.wallet.transfer.assert_called_once()
        self.assertEqual("0.00000011", self.earn.create_uni_lend.call_args.args[0].min_rate)
        self.assertEqual("done", self.state()["stage"])

    def test_rejected_lend_rate_keeps_existing_transfer_ready_for_lending(self):
        self.balances()
        self.earn.create_uni_lend.side_effect = GateApiException(
            "INVALID_PARAM_VALUE", "Invalid request parameter `min_rate` value: 1.1E-7",
            exp=ApiException(status=400),
        )
        with self.assertRaises(GateApiException):
            self.run_flow("futures_to_earn")
        self.assertEqual("lend_ready", self.state()["stage"])
        self.assertEqual("123", self.state()["tx_id"])
        self.earn.create_uni_lend.side_effect = None
        self.run_flow("futures_to_earn")
        self.wallet.transfer.assert_called_once()
        self.assertEqual("done", self.state()["stage"])

    def test_pending_transfer_only_polls_on_restart(self):
        self.wallet.get_transfer.side_effect = lambda *a, **k: self.detail("pending")
        with self.assertRaises(TimeoutError):
            self.run_flow("futures_to_earn")
        self.assertEqual("transfer_wait", self.state()["stage"])
        self.wallet.get_transfer.side_effect = lambda *a, **k: self.detail("success")
        self.client.call_api.return_value = [{"currency": "USDT", "available": "10"}]
        self.run_flow("futures_to_earn")
        self.wallet.transfer.assert_called_once()

    def test_resume_existing_transfer_lends_with_null_settle_and_available_amount(self):
        self.client.call_api.return_value = [{"currency": "USDT", "available": "5"}]
        self.wallet.get_transfer.side_effect = lambda *a, **k: self.detail("pending")
        with self.assertRaises(TimeoutError):
            self.run_flow("futures_to_earn")

        def completed(*args, **kwargs):
            result = self.detail("SUCCESS")
            result.settle = None
            result.from_account = "FUTURES"
            result.currency = "usdt"
            return result

        self.wallet.get_transfer.side_effect = completed
        self.client.call_api.return_value = [{"currency": "USDT", "available": "10"}]
        self.run_flow("futures_to_earn")
        self.wallet.transfer.assert_called_once()
        self.earn.create_uni_lend.assert_called_once()
        self.assertEqual("lend", self.earn.create_uni_lend.call_args.args[0].type)
        self.assertEqual("done", self.state()["stage"])

    def test_reset_archives_finished_task_and_starts_one_new_task(self):
        self.balances()
        self.run_flow()
        original = self.state()
        self.balances()
        self.run_flow(reset=True)
        backup, = self.path.parent.glob("state.*.bak.json")
        self.assertEqual(original, json.loads(backup.read_text(encoding="utf-8")))
        self.assertEqual(2, self.wallet.transfer.call_count)
        self.assertEqual(2, self.earn.create_uni_lend.call_count)

    def test_reset_archives_pending_transfer_and_starts_fresh(self):
        self.wallet.get_transfer.side_effect = lambda *a, **k: self.detail("pending")
        with self.assertRaises(TimeoutError):
            self.run_flow("futures_to_earn")
        before = self.state()
        self.wallet.get_transfer.reset_mock()
        self.wallet.transfer.return_value = NS(tx_id=456)

        def new_detail(tx_id, **kwargs):
            self.assertEqual("456", tx_id)
            result = self.detail("success")
            result.tx_id = tx_id
            return result

        self.wallet.get_transfer.side_effect = new_detail
        self.balances()
        self.run_flow("futures_to_earn", reset=True)
        backup, = self.path.parent.glob("state.*.bak.json")
        self.assertEqual(before, json.loads(backup.read_text(encoding="utf-8")))
        self.assertEqual("456", self.state()["tx_id"])
        self.assertEqual(2, self.wallet.transfer.call_count)
        self.earn.create_uni_lend.assert_called_once()

    def test_transfer_failure_does_not_lend(self):
        self.wallet.get_transfer.side_effect = lambda *a, **k: self.detail("fail")
        with self.assertRaisesRegex(RuntimeError, "划转失败"):
            self.run_flow("futures_to_earn")
        self.earn.create_uni_lend.assert_not_called()

    def test_wrong_transaction_id_cannot_trigger_lending(self):
        def wrong_detail(*a, **k):
            result = self.detail("success")
            result.tx_id = "other-transaction"
            return result
        self.wallet.get_transfer.side_effect = wrong_detail
        with self.assertRaisesRegex(RuntimeError, "不一致"):
            self.run_flow("futures_to_earn")
        self.earn.create_uni_lend.assert_not_called()

    def test_insufficient_funds_and_unified_account_do_not_transfer(self):
        for account in (NS(margin_mode=1, available="100"), NS(margin_mode=0, available="1")):
            with self.subTest(account=account):
                self.futures.list_futures_accounts.return_value = account
                with self.assertRaises((ValueError, RuntimeError)):
                    self.run_flow("futures_to_earn")
                self.wallet.transfer.assert_not_called()

    def test_changed_amount_or_key_cannot_reuse_checkpoint(self):
        with self.assertRaises(TimeoutError):
            self.run_flow()
        with self.assertRaisesRegex(RuntimeError, "其他金额"):
            flow.run_steps(self.client, "earn_to_futures", Decimal("11"), None, self.path,
                           wait_timeout=0, poll_interval=2)
        self.client.configuration.key = "another-dummy-key"
        with self.assertRaisesRegex(RuntimeError, "其他金额"):
            self.run_flow()

    def test_low_level_balance_uses_signed_get_and_only_usdt(self):
        self.client.call_api.return_value = [{"currency": "USDT", "available": "1.25"},
                                             {"currency": "BTC", "available": "9"}]
        self.assertEqual(Decimal("1.25"), flow.spot_available(self.client))
        self.assertEqual(("/spot/accounts", "GET"), self.client.call_api.call_args.args[:2])
        self.assertEqual(["apiv4"], self.client.call_api.call_args.kwargs["auth_settings"])

    def test_default_preview_makes_no_api_client(self):
        with patch.object(flow, "load_env_file"), patch.dict("os.environ", {}, clear=True), \
                patch.object(flow, "ApiClient") as client:
            flow.execute_transfer("earn_to_futures", "10", self.path)
            flow.execute_transfer("futures_to_earn", "10", self.path)
            client.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_both_directions_require_execute_and_ignore_old_confirmation_variable(self):
        from example import simple_earn_to_futures, simple_futures_to_earn

        for entry, direction in ((simple_earn_to_futures, "earn_to_futures"),
                                 (simple_futures_to_earn, "futures_to_earn")):
            with self.subTest(direction=direction), patch.object(entry, "STATE_FILE", self.path), \
                    patch.object(flow, "load_env_file"), patch.object(flow, "ApiClient") as client, \
                    patch.object(flow, "StrategyProcessLock"), patch.object(flow, "run_steps") as steps, \
                    patch.dict("os.environ", {"GATE_TRANSFER_CONFIRM": direction.upper()}, clear=True):
                entry.main([])
                client.assert_not_called()
                with patch.dict("os.environ", {"GATE_API_KEY": "test-key", "GATE_API_SECRET": "test-secret"}, clear=True):
                    entry.main(["--execute"])
                self.assertEqual(direction, steps.call_args.args[1])

    def test_invalid_amount_rejected_before_api(self):
        for value in ("0", "-1", "NaN", "Infinity", "0.000000001"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                flow.execute_transfer("earn_to_futures", value, self.path)


if __name__ == "__main__":
    unittest.main()
