from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from utils.indicators import Position
from utils.trade_manager import TradeManager


def _make_manager() -> TradeManager:
    return TradeManager(MagicMock(), poll_interval_secs=60)


def _register_kwargs(**overrides) -> dict:
    base = dict(
        symbol="BTCUSDT",
        position=Position.LONG,
        size=Decimal("0.01"),
        entry_price=Decimal("50000"),
        tick_size=Decimal("0.1"),
        stop_ids=[101, 102],
        sl_limit_price=Decimal("49250"),
        sl_market_price=Decimal("49100"),
        ttp_id=201,
        tp_limit_id=301,
    )
    base.update(overrides)
    return base


def _patch_pnl(pnl: str = "0"):
    """Patch the P&L query so tests don't need a real client."""
    return patch(
        "utils.trade_manager.TradeManager._query_realized_pnl",
        return_value=Decimal(pnl),
    )


def _patch_open_ids(ids: set):
    """Patch _fetch_open_order_ids to return a fixed set."""
    return patch(
        "utils.trade_manager.TradeManager._fetch_open_order_ids",
        return_value=ids,
    )


# ---------------------------------------------------------------------------
# get_position / get_size
# ---------------------------------------------------------------------------

class TestGetters:
    def test_unregistered_symbol_returns_none(self):
        mgr = _make_manager()
        assert mgr.get_position("BTCUSDT") == Position.NONE
        assert mgr.get_size("BTCUSDT") == Decimal("0")

    def test_registered_position_returned(self):
        mgr = _make_manager()
        mgr.register_trade(**_register_kwargs(position=Position.SHORT))
        assert mgr.get_position("BTCUSDT") == Position.SHORT

    def test_registered_size_returned(self):
        mgr = _make_manager()
        mgr.register_trade(**_register_kwargs(size=Decimal("0.05")))
        assert mgr.get_size("BTCUSDT") == Decimal("0.05")


# ---------------------------------------------------------------------------
# close_trade
# ---------------------------------------------------------------------------

class TestCloseTrade:
    def test_position_becomes_none_after_close(self):
        mgr = _make_manager()
        mgr.register_trade(**_register_kwargs())
        mgr.close_trade("BTCUSDT")
        assert mgr.get_position("BTCUSDT") == Position.NONE

    def test_no_cancel_calls_made(self):
        # close_trade only removes state; Binance auto-cancels reduceOnly orders
        mgr = _make_manager()
        mgr.register_trade(**_register_kwargs(stop_ids=[101, 102], ttp_id=201, tp_limit_id=301))
        with patch("utils.trade_manager.algo_orders.cancel_algo_order") as mock_algo, \
             patch("utils.trade_manager.orders.cancel_order") as mock_order:
            mgr.close_trade("BTCUSDT")
        mock_algo.assert_not_called()
        mock_order.assert_not_called()

    def test_noop_when_not_registered(self):
        mgr = _make_manager()
        mgr.close_trade("BTCUSDT")  # must not raise

    def test_double_close_is_safe(self):
        mgr = _make_manager()
        mgr.register_trade(**_register_kwargs())
        mgr.close_trade("BTCUSDT")
        mgr.close_trade("BTCUSDT")  # must not raise


# ---------------------------------------------------------------------------
# _reconcile — external close
# ---------------------------------------------------------------------------

class TestReconcileExternalClose:
    def test_clears_state_when_binance_size_is_zero(self):
        mgr = _make_manager()
        mgr.register_trade(**_register_kwargs())
        with patch("utils.trade_manager.account.get_futures_positions", return_value=[]), \
             _patch_open_ids(set()), \
             patch("utils.trade_manager.algo_orders.cancel_algo_order"), \
             patch("utils.trade_manager.orders.cancel_order"), \
             _patch_pnl("0"):
            mgr._reconcile("BTCUSDT")
        assert mgr.get_position("BTCUSDT") == Position.NONE

    def test_no_cancel_calls_when_all_orders_already_gone(self):
        # All exit orders fired simultaneously — nothing left to cancel.
        mgr = _make_manager()
        mgr.register_trade(**_register_kwargs(stop_ids=[101, 102], ttp_id=201, tp_limit_id=301))
        with patch("utils.trade_manager.account.get_futures_positions", return_value=[]), \
             _patch_open_ids(set()), \
             patch("utils.trade_manager.algo_orders.cancel_algo_order") as mock_algo, \
             patch("utils.trade_manager.orders.cancel_order") as mock_order, \
             _patch_pnl("0"):
            mgr._reconcile("BTCUSDT")
        mock_algo.assert_not_called()
        mock_order.assert_not_called()

    def test_cancels_all_leftover_orders_on_external_close(self):
        mgr = _make_manager()
        mgr.register_trade(**_register_kwargs(stop_ids=[101, 102], ttp_id=201, tp_limit_id=301))
        # Simulate: TP limit (301) fired — stops (101,102) and trailing TP (201) are leftovers still open
        open_ids = {101, 102, 201}
        with patch("utils.trade_manager.account.get_futures_positions", return_value=[]), \
             _patch_open_ids(open_ids), \
             patch("utils.trade_manager.algo_orders.cancel_algo_order") as mock_algo, \
             patch("utils.trade_manager.orders.cancel_order") as mock_order, \
             _patch_pnl("50"):
            mgr._reconcile("BTCUSDT")

        cancelled_algo_ids = {c.args[2] for c in mock_algo.call_args_list}
        assert {101, 102, 201} == cancelled_algo_ids
        # tp_limit_id=301 already gone (it fired), cancel_order only called for it if it's a leftover
        # here it's NOT in open_ids so it won't be in the leftover list — cancel_order should NOT fire
        mock_order.assert_not_called()

    def test_tp_limit_cancelled_when_stop_fires(self):
        mgr = _make_manager()
        mgr.register_trade(**_register_kwargs(stop_ids=[101, 102], ttp_id=201, tp_limit_id=301))
        # Simulate: stop-limit (101) fired — TP limit (301) and trailing TP (201) are leftovers
        open_ids = {102, 201, 301}
        with patch("utils.trade_manager.account.get_futures_positions", return_value=[]), \
             _patch_open_ids(open_ids), \
             patch("utils.trade_manager.algo_orders.cancel_algo_order") as mock_algo, \
             patch("utils.trade_manager.orders.cancel_order") as mock_order, \
             _patch_pnl("-20"):
            mgr._reconcile("BTCUSDT")

        cancelled_algo_ids = {c.args[2] for c in mock_algo.call_args_list}
        assert {102, 201}.issubset(cancelled_algo_ids)
        assert mock_order.call_args.args[2] == 301

    def test_no_action_for_unregistered_symbol(self):
        mgr = _make_manager()
        with patch("utils.trade_manager.account.get_futures_positions", return_value=[]), \
             patch("utils.trade_manager.algo_orders.cancel_algo_order") as mock_cancel:
            mgr._reconcile("BTCUSDT")
        mock_cancel.assert_not_called()

    def test_pnl_logged_on_close(self):
        mgr = _make_manager()
        mgr.register_trade(**_register_kwargs())
        with patch("utils.trade_manager.account.get_futures_positions", return_value=[]), \
             _patch_open_ids(set()), \
             patch("utils.trade_manager.algo_orders.cancel_algo_order"), \
             patch("utils.trade_manager.orders.cancel_order"), \
             patch("utils.trade_manager.TradeManager._query_realized_pnl",
                   return_value=Decimal("75.50")) as mock_pnl:
            mgr._reconcile("BTCUSDT")
        mock_pnl.assert_called_once()


# ---------------------------------------------------------------------------
# _reconcile — partial TP fill
# ---------------------------------------------------------------------------

class TestReconcilePartialFill:
    def _reduced_pos(self, size: Decimal) -> list[dict]:
        return [{"amount": size}]

    def test_size_updated_after_partial_fill(self):
        mgr = _make_manager()
        mgr.register_trade(**_register_kwargs(size=Decimal("0.01")))
        reduced = Decimal("0.005")
        with patch("utils.trade_manager.account.get_futures_positions", return_value=self._reduced_pos(reduced)), \
             patch("utils.trade_manager.algo_orders.cancel_algo_order"), \
             patch("utils.trade_manager.algo_orders.place_stop_limit_order", return_value={"order_id": 901}), \
             patch("utils.trade_manager.algo_orders.place_stop_market_order", return_value={"order_id": 902}), \
             _patch_open_ids({901, 902}), \
             _patch_pnl("25"):
            mgr._reconcile("BTCUSDT")
        assert mgr.get_size("BTCUSDT") == reduced

    def test_position_still_open_after_partial_fill(self):
        mgr = _make_manager()
        mgr.register_trade(**_register_kwargs(size=Decimal("0.01"), position=Position.LONG))
        reduced = Decimal("0.005")
        with patch("utils.trade_manager.account.get_futures_positions", return_value=self._reduced_pos(reduced)), \
             patch("utils.trade_manager.algo_orders.cancel_algo_order"), \
             patch("utils.trade_manager.algo_orders.place_stop_limit_order", return_value={"order_id": 901}), \
             patch("utils.trade_manager.algo_orders.place_stop_market_order", return_value={"order_id": 902}), \
             _patch_open_ids({901, 902}), \
             _patch_pnl("0"):
            mgr._reconcile("BTCUSDT")
        assert mgr.get_position("BTCUSDT") == Position.LONG

    def test_stops_cancelled_and_replaced_at_new_size(self):
        mgr = _make_manager()
        mgr.register_trade(**_register_kwargs(size=Decimal("0.01"), stop_ids=[101, 102]))
        reduced = Decimal("0.005")
        with patch("utils.trade_manager.account.get_futures_positions", return_value=self._reduced_pos(reduced)), \
             patch("utils.trade_manager.algo_orders.cancel_algo_order") as mock_cancel, \
             patch("utils.trade_manager.algo_orders.place_stop_limit_order", return_value={"order_id": 901}) as mock_sl, \
             patch("utils.trade_manager.algo_orders.place_stop_market_order", return_value={"order_id": 902}) as mock_sm, \
             _patch_open_ids({901, 902}), \
             _patch_pnl("0"):
            mgr._reconcile("BTCUSDT")

        cancelled = {c.args[2] for c in mock_cancel.call_args_list}
        assert {101, 102}.issubset(cancelled)
        assert mock_sl.call_args.args[3] == reduced
        assert mock_sm.call_args.args[3] == reduced

    def test_new_stop_ids_stored_in_state(self):
        mgr = _make_manager()
        mgr.register_trade(**_register_kwargs(size=Decimal("0.01"), stop_ids=[101, 102]))
        reduced = Decimal("0.005")
        with patch("utils.trade_manager.account.get_futures_positions", return_value=self._reduced_pos(reduced)), \
             patch("utils.trade_manager.algo_orders.cancel_algo_order"), \
             patch("utils.trade_manager.algo_orders.place_stop_limit_order", return_value={"order_id": 901}), \
             patch("utils.trade_manager.algo_orders.place_stop_market_order", return_value={"order_id": 902}), \
             _patch_open_ids({901, 902}), \
             _patch_pnl("0"):
            mgr._reconcile("BTCUSDT")

        # new stop IDs should be tracked so the next reconcile can cancel them if needed
        with mgr._lock:
            assert set(mgr._states["BTCUSDT"].stop_ids) == {901, 902}

    def test_no_action_when_size_unchanged(self):
        mgr = _make_manager()
        mgr.register_trade(**_register_kwargs(size=Decimal("0.01")))
        # 0 unrealized P&L — below the 1% trigger
        with patch("utils.trade_manager.account.get_futures_positions",
                   return_value=[{"amount": Decimal("0.01"), "unrealized_pnl": Decimal("0")}]), \
             patch("utils.trade_manager.algo_orders.cancel_algo_order") as mock_cancel:
            mgr._reconcile("BTCUSDT")
        mock_cancel.assert_not_called()

    def test_residual_below_min_notional_closes_directly(self):
        # entry=50000, residual=0.0001 BNB → notional=5 USDT < default 10 USDT threshold
        mgr = _make_manager()
        mgr.register_trade(**_register_kwargs(size=Decimal("0.01"), entry_price=Decimal("50000")))
        residual = Decimal("0.0001")
        with patch("utils.trade_manager.account.get_futures_positions", return_value=self._reduced_pos(residual)), \
             patch("utils.trade_manager.positions.close_position") as mock_close, \
             patch("utils.trade_manager.algo_orders.place_stop_limit_order") as mock_sl, \
             _patch_pnl("10"):
            mgr._reconcile("BTCUSDT")
        mock_close.assert_called_once()
        mock_sl.assert_not_called()
        assert mgr.get_position("BTCUSDT") == Position.NONE

    def test_recovered_position_skips_stop_replacement(self):
        mgr = _make_manager()
        mgr.register_trade(**_register_kwargs(size=Decimal("0.01"), has_order_details=False))
        reduced = Decimal("0.005")
        with patch("utils.trade_manager.account.get_futures_positions", return_value=self._reduced_pos(reduced)), \
             patch("utils.trade_manager.algo_orders.cancel_algo_order") as mock_cancel, \
             patch("utils.trade_manager.algo_orders.place_stop_limit_order") as mock_sl, \
             _patch_pnl("0"):
            mgr._reconcile("BTCUSDT")

        mock_cancel.assert_not_called()
        mock_sl.assert_not_called()
        assert mgr.get_size("BTCUSDT") == reduced


# ---------------------------------------------------------------------------
# _identify_fired_order
# ---------------------------------------------------------------------------

class TestIdentifyFiredOrder:
    def test_identifies_tp_limit_as_fired(self):
        mgr = _make_manager()
        mgr.register_trade(**_register_kwargs(tp_limit_id=301, stop_ids=[101, 102], ttp_id=201))
        with mgr._lock:
            state = mgr._states["BTCUSDT"]
        # TP limit gone, stops still open
        result = mgr._identify_fired_order(state, open_ids={101, 102, 201})
        assert "TP limit" in result
        assert "301" in result

    def test_identifies_stop_limit_as_fired(self):
        mgr = _make_manager()
        mgr.register_trade(**_register_kwargs(stop_ids=[101, 102], ttp_id=201, tp_limit_id=301))
        with mgr._lock:
            state = mgr._states["BTCUSDT"]
        # stop-limit gone, others open
        result = mgr._identify_fired_order(state, open_ids={102, 201, 301})
        assert "stop-limit" in result

    def test_unknown_for_recovered_position(self):
        mgr = _make_manager()
        mgr.register_trade(**_register_kwargs(has_order_details=False))
        with mgr._lock:
            state = mgr._states["BTCUSDT"]
        result = mgr._identify_fired_order(state, open_ids=set())
        assert "unknown" in result
        assert "recovered" in result


# ---------------------------------------------------------------------------
# SL profit-lock milestone
# ---------------------------------------------------------------------------

class TestSlMilestone:
    # entry=50000, size=0.01 → notional=500 USDT
    # 1% profit = 5 USDT unrealized_pnl
    # 0.5% profit = 2.5 USDT

    def _pos(self, size: Decimal, unrealized_pnl: Decimal) -> list[dict]:
        return [{"amount": size, "unrealized_pnl": unrealized_pnl}]

    def test_no_sl_move_below_trigger(self):
        mgr = _make_manager()
        mgr.register_trade(**_register_kwargs(size=Decimal("0.01"), entry_price=Decimal("50000")))
        # 0.5% profit (2.5 USDT) — below 1% trigger
        with patch("utils.trade_manager.account.get_futures_positions",
                   return_value=self._pos(Decimal("0.01"), Decimal("2.5"))), \
             patch("utils.trade_manager.algo_orders.cancel_algo_order") as mock_cancel, \
             patch("utils.trade_manager.algo_orders.place_stop_limit_order") as mock_sl:
            mgr._reconcile("BTCUSDT")
        mock_cancel.assert_not_called()
        mock_sl.assert_not_called()

    def test_sl_move_triggered_at_threshold(self):
        mgr = _make_manager()
        mgr.register_trade(**_register_kwargs(
            size=Decimal("0.01"), entry_price=Decimal("50000"), stop_ids=[101, 102]
        ))
        # 1.0% profit (5 USDT) — exactly at trigger
        with patch("utils.trade_manager.account.get_futures_positions",
                   return_value=self._pos(Decimal("0.01"), Decimal("5"))), \
             patch("utils.trade_manager.algo_orders.place_stop_limit_order",
                   return_value={"order_id": 901}) as mock_new_sl, \
             patch("utils.trade_manager.algo_orders.place_stop_market_order",
                   return_value={"order_id": 902}) as mock_new_sm, \
             patch("utils.trade_manager.algo_orders.cancel_algo_order") as mock_cancel, \
             patch("utils.trade_manager.TradeManager._fetch_open_order_ids", return_value={901, 902}):
            mgr._reconcile("BTCUSDT")
        mock_new_sl.assert_called_once()
        mock_new_sm.assert_called_once()
        assert mock_cancel.call_count == 2  # old stop-limit + old stop-market

    def test_new_stops_placed_before_old_cancelled(self):
        """Verify place calls happen before cancel calls to avoid an unprotected window."""
        call_order = []
        mgr = _make_manager()
        mgr.register_trade(**_register_kwargs(
            size=Decimal("0.01"), entry_price=Decimal("50000"), stop_ids=[101, 102]
        ))

        def record_place_sl(*args, **kwargs):
            call_order.append("place_sl")
            return {"order_id": 901}

        def record_place_sm(*args, **kwargs):
            call_order.append("place_sm")
            return {"order_id": 902}

        def record_cancel(client, symbol, algo_id):
            call_order.append(f"cancel_{algo_id}")

        with patch("utils.trade_manager.account.get_futures_positions",
                   return_value=self._pos(Decimal("0.01"), Decimal("5"))), \
             patch("utils.trade_manager.algo_orders.place_stop_limit_order", side_effect=record_place_sl), \
             patch("utils.trade_manager.algo_orders.place_stop_market_order", side_effect=record_place_sm), \
             patch("utils.trade_manager.algo_orders.cancel_algo_order", side_effect=record_cancel), \
             patch("utils.trade_manager.TradeManager._fetch_open_order_ids", return_value={901, 902}):
            mgr._reconcile("BTCUSDT")

        assert call_order[0] == "place_sl"
        assert call_order[1] == "place_sm"
        assert all("cancel" in c for c in call_order[2:])

    def test_sl_moved_flag_prevents_second_move(self):
        mgr = _make_manager()
        mgr.register_trade(**_register_kwargs(
            size=Decimal("0.01"), entry_price=Decimal("50000"), stop_ids=[101, 102]
        ))
        pos = self._pos(Decimal("0.01"), Decimal("5"))
        with patch("utils.trade_manager.account.get_futures_positions", return_value=pos), \
             patch("utils.trade_manager.algo_orders.place_stop_limit_order",
                   return_value={"order_id": 901}), \
             patch("utils.trade_manager.algo_orders.place_stop_market_order",
                   return_value={"order_id": 902}), \
             patch("utils.trade_manager.algo_orders.cancel_algo_order"), \
             patch("utils.trade_manager.TradeManager._fetch_open_order_ids", return_value={901, 902}):
            mgr._reconcile("BTCUSDT")  # first poll — triggers move

        with patch("utils.trade_manager.account.get_futures_positions", return_value=pos), \
             patch("utils.trade_manager.algo_orders.place_stop_limit_order") as mock_sl2, \
             patch("utils.trade_manager.algo_orders.cancel_algo_order"):
            mgr._reconcile("BTCUSDT")  # second poll — must not trigger again

        mock_sl2.assert_not_called()

    def test_sl_move_short_position(self):
        mgr = _make_manager()
        mgr.register_trade(**_register_kwargs(
            position=Position.SHORT,
            size=Decimal("0.01"),
            entry_price=Decimal("50000"),
            stop_ids=[101, 102],
        ))
        # 1% profit for short = positive 5 USDT (Binance sign: profit is positive for short too)
        with patch("utils.trade_manager.account.get_futures_positions",
                   return_value=self._pos(Decimal("-0.01"), Decimal("5"))), \
             patch("utils.trade_manager.algo_orders.place_stop_limit_order",
                   return_value={"order_id": 901}) as mock_sl, \
             patch("utils.trade_manager.algo_orders.place_stop_market_order",
                   return_value={"order_id": 902}), \
             patch("utils.trade_manager.algo_orders.cancel_algo_order"), \
             patch("utils.trade_manager.TradeManager._fetch_open_order_ids", return_value={901, 902}):
            mgr._reconcile("BTCUSDT")
        # For short, new stop-limit should be below entry (lock in profit at -0.5%)
        placed_trigger = mock_sl.call_args.args[4]
        assert placed_trigger < Decimal("50000")


# ---------------------------------------------------------------------------
# tick_stagnation
# ---------------------------------------------------------------------------

class TestTickStagnation:
    """Tests for the rolling momentum-decay exit check.

    Entry price is always 50000. Default stagnation window is 4 candles with a
    2% minimum price move. Weak ADX = 20 (below min 25). Weak RSI for LONG = 45
    (below rsi_long_low 50). Weak RSI for SHORT = 55 (above rsi_short_high 50).
    """

    def _tick(
        self,
        mgr: TradeManager,
        symbol: str = "BTCUSDT",
        price: str = "50000",
        adx: str = "20",
        rsi_val: str = "45",
        min_adx: str = "25",
        rsi_long_low: str = "50",
        rsi_short_high: str = "50",
        candles: int = 4,
        min_pct: str = "2.0",
        reversal_pct: str = "0.15",
    ) -> bool:
        return mgr.tick_stagnation(
            symbol=symbol,
            current_price=Decimal(price),   # Decimal — used in Decimal arithmetic with checkpoint_price
            current_adx=float(adx),
            current_rsi=float(rsi_val),
            min_adx=float(min_adx),
            rsi_long_low=float(rsi_long_low),
            rsi_short_high=float(rsi_short_high),
            stagnation_candles=candles,
            stagnation_min_pct=float(min_pct),
            stagnation_reversal_pct=float(reversal_pct),
        )

    def test_returns_false_for_unregistered_symbol(self):
        mgr = _make_manager()
        assert self._tick(mgr) is False

    def test_does_not_fire_before_n_candles(self):
        mgr = _make_manager()
        mgr.register_trade(**_register_kwargs(entry_price=Decimal("50000")))
        # 3 ticks with all conditions bad — window not reached yet
        for _ in range(3):
            result = self._tick(mgr)
        assert result is False

    def test_fires_when_all_three_conditions_fail(self):
        mgr = _make_manager()
        mgr.register_trade(**_register_kwargs(entry_price=Decimal("50000")))
        for _ in range(3):
            self._tick(mgr)
        # Candle 4: price moved 1% (< 2%), ADX=20 (< 25), RSI=45 (< 50) → stagnation
        result = self._tick(mgr, price="50500")
        assert result is True

    def test_does_not_fire_when_price_moved_enough(self):
        mgr = _make_manager()
        mgr.register_trade(**_register_kwargs(entry_price=Decimal("50000")))
        for _ in range(3):
            self._tick(mgr)
        # 2.5% move exceeds stagnation_min_pct=2.0 — price condition not met
        result = self._tick(mgr, price="51250")
        assert result is False

    def test_does_not_fire_when_adx_still_trending(self):
        mgr = _make_manager()
        mgr.register_trade(**_register_kwargs(entry_price=Decimal("50000")))
        for _ in range(3):
            self._tick(mgr, adx="30")
        # ADX=30 > min_adx=25 — trend still strong, must not exit
        result = self._tick(mgr, price="50500", adx="30")
        assert result is False

    def test_does_not_fire_when_rsi_in_momentum_zone(self):
        mgr = _make_manager()
        mgr.register_trade(**_register_kwargs(entry_price=Decimal("50000")))
        for _ in range(3):
            self._tick(mgr, rsi_val="60")
        # RSI=60 >= rsi_long_low=50 — momentum still valid for long, must not exit
        result = self._tick(mgr, price="50500", rsi_val="60")
        assert result is False

    def test_checkpoint_resets_on_passing_window(self):
        mgr = _make_manager()
        mgr.register_trade(**_register_kwargs(entry_price=Decimal("50000")))
        # First window: 3% move (> 2%) → passes, checkpoint should update to 51500
        for _ in range(3):
            self._tick(mgr)
        self._tick(mgr, price="51500")  # price good — no stagnation, checkpoint resets
        with mgr._lock:
            assert mgr._states["BTCUSDT"].checkpoint_price == Decimal("51500")

    def test_second_window_measured_from_checkpoint_not_entry(self):
        mgr = _make_manager()
        mgr.register_trade(**_register_kwargs(entry_price=Decimal("50000")))
        # First window passes: price moves from 50000 → 51500 (3%)
        for _ in range(3):
            self._tick(mgr)
        self._tick(mgr, price="51500")
        # Second window: price barely moves from new checkpoint 51500 (0.19%) → stagnation
        for _ in range(3):
            self._tick(mgr, price="51500")
        result = self._tick(mgr, price="51600")  # (51600-51500)/51500 ≈ 0.19%
        assert result is True

    def test_short_position_stagnation(self):
        mgr = _make_manager()
        mgr.register_trade(**_register_kwargs(
            position=Position.SHORT, entry_price=Decimal("50000")
        ))
        for _ in range(3):
            self._tick(mgr, rsi_val="55")  # RSI > rsi_short_high=50 → rsi_weak for short
        # Price moved only 0.5% down (< 2%), ADX weak, RSI out of zone → stagnation
        result = self._tick(mgr, price="49750", rsi_val="55")
        assert result is True

    def test_short_no_stagnation_when_price_moved(self):
        mgr = _make_manager()
        mgr.register_trade(**_register_kwargs(
            position=Position.SHORT, entry_price=Decimal("50000")
        ))
        for _ in range(3):
            self._tick(mgr, rsi_val="55")
        # Price moved 3% down (> 2%) — price condition not met, no exit
        result = self._tick(mgr, price="48500", rsi_val="55")
        assert result is False

    def test_short_rsi_in_zone_prevents_exit(self):
        mgr = _make_manager()
        mgr.register_trade(**_register_kwargs(
            position=Position.SHORT, entry_price=Decimal("50000")
        ))
        for _ in range(3):
            self._tick(mgr, rsi_val="40")  # RSI=40 <= rsi_short_high=50 → rsi_weak=False
        result = self._tick(mgr, price="49750", rsi_val="40")
        assert result is False

    def test_reversal_exit_long_fires_regardless_of_adx(self):
        mgr = _make_manager()
        mgr.register_trade(**_register_kwargs(entry_price=Decimal("50000")))
        for _ in range(3):
            self._tick(mgr, adx="30", rsi_val="60")
        # Price dropped 0.2% below checkpoint — exceeds default 0.15% threshold
        result = self._tick(mgr, price="49900", adx="30", rsi_val="60")
        assert result is True

    def test_reversal_exit_short_fires_regardless_of_adx(self):
        mgr = _make_manager()
        mgr.register_trade(**_register_kwargs(
            position=Position.SHORT, entry_price=Decimal("50000")
        ))
        for _ in range(3):
            self._tick(mgr, adx="30", rsi_val="40")
        # Price rose 0.2% above checkpoint — exceeds default 0.15% threshold
        result = self._tick(mgr, price="50100", adx="30", rsi_val="40")
        assert result is True

    def test_no_reversal_exit_when_price_exactly_at_checkpoint(self):
        mgr = _make_manager()
        mgr.register_trade(**_register_kwargs(entry_price=Decimal("50000")))
        for _ in range(3):
            self._tick(mgr, adx="30", rsi_val="60")
        # price_pct = 0 — stagnation blocked by strong ADX, reversal requires > threshold
        result = self._tick(mgr, price="50000", adx="30", rsi_val="60")
        assert result is False

    def test_reversal_does_not_fire_below_threshold(self):
        mgr = _make_manager()
        mgr.register_trade(**_register_kwargs(entry_price=Decimal("50000")))
        for _ in range(3):
            self._tick(mgr, adx="30", rsi_val="60")
        # 0.1% drop — below default 0.15% threshold, must not fire
        result = self._tick(mgr, price="49950", adx="30", rsi_val="60")
        assert result is False

    def test_reversal_threshold_is_configurable(self):
        mgr = _make_manager()
        mgr.register_trade(**_register_kwargs(entry_price=Decimal("50000")))
        for _ in range(3):
            self._tick(mgr, adx="30", rsi_val="60", reversal_pct="0.30")
        # 0.25% drop — below custom 0.30% threshold, must not fire
        result = self._tick(mgr, price="49875", adx="30", rsi_val="60", reversal_pct="0.30")
        assert result is False
