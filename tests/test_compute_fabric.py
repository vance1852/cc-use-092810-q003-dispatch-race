from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from battery_logistics.api import JsonApplication
from battery_logistics.clock import FrozenClock
from battery_logistics.errors import Conflict, Forbidden, InvalidState
from battery_logistics.planning import AllocationRequest, PricePoint, allocate_capacity, latest_streak
from battery_logistics.service import SupplyService
from battery_logistics.risk import DemandBucket, inventory_coverage, mark_to_market, supply_gap
from battery_logistics.storage import connect


class PlanningTests(unittest.TestCase):
    def test_latest_down_streak_uses_first_close_as_base(self) -> None:
        streak = latest_streak([
            PricePoint("2026-09-18", Decimal("108")),
            PricePoint("2026-09-19", Decimal("105")),
            PricePoint("2026-09-20", Decimal("102")),
            PricePoint("2026-09-21", Decimal("98")),
        ])
        self.assertEqual(streak.direction, "down")
        self.assertEqual(streak.sessions, 4)
        self.assertEqual(streak.start_date, "2026-09-18")
        self.assertEqual(streak.end_close, Decimal("98"))

    def test_allocation_is_stable_and_does_not_exceed_capacity(self) -> None:
        rows = allocate_capacity(Decimal("100"), [
            AllocationRequest("later", Decimal("80"), 20, "2026-09-24T09:00:00Z"),
            AllocationRequest("first", Decimal("70"), 10, "2026-09-24T10:00:00Z"),
        ])
        self.assertEqual(rows[0]["nomination_id"], "first")
        self.assertEqual(rows[0]["allocated_energy_kwh"], "70.000")
        self.assertEqual(rows[1]["allocated_energy_kwh"], "30.000")

    def test_inventory_coverage_and_supply_gap(self) -> None:
        coverage = inventory_coverage(
            [{"facility_id": "inference-pool", "product": "battery-lfp-standard", "available_energy_kwh": "250"}],
            [DemandBucket("inference-pool", "battery-lfp-standard", Decimal("100"), Decimal("20"))],
        )
        self.assertEqual(coverage[0]["coverage_days"], "2.30")
        self.assertTrue(coverage[0]["below_three_days"])
        gap = supply_gap(
            opening_inventory=Decimal("100"),
            confirmed_inbound=Decimal("30"),
            forecast_demand=Decimal("120"),
            protected_reserve=Decimal("40"),
        )
        self.assertEqual(gap["supply_gap"], "30.000")

    def test_mark_to_market_groups_deterministically(self) -> None:
        result = mark_to_market(
            [{"position_id": "p1", "market_index": "PEAK_VALLEY", "quantity_energy_kwh": "100", "entry_price_cny": "105"}],
            {"PEAK_VALLEY": Decimal("98")},
        )
        self.assertEqual(result["unrealized_pnl_cny"], "-700.00")


class SupplyServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = SupplyService(self.connection, self.clock)
        for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
            self.service.create_user(user_id, user_id, role)
        self.service.create_facility("plan", {"facility_id": "cluster-a", "name": "北部储能资产平台", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_energy_kwh": "500000"})
        self.service.create_facility("plan", {"facility_id": "pool-b", "name": "东部推理池", "kind": "inference-pool", "timezone": "Asia/Shanghai", "capacity_energy_kwh": "800000"})
        self.service.create_route("plan", {"route_id": "fabric-a-b", "origin_id": "cluster-a", "destination_id": "pool-b", "product": "battery-lfp-high", "daily_capacity": "100000", "loss_basis_points": 25, "transit_hours": 36})

    def tearDown(self) -> None:
        self.connection.close()

    def quote(self, day: int, close: str) -> dict[str, object]:
        return self.service.record_quote("plan", {"market_index": "PEAK_VALLEY", "trade_date": f"2026-09-{day}", "close_cny": close, "source_revision": f"r-{day}", "observed_at": f"2026-09-{day}T21:00:00Z"})

    def test_quote_revisions_preserve_history(self) -> None:
        first = self.quote(23, "98")
        second = self.service.record_quote("plan", {"market_index": "PEAK_VALLEY", "trade_date": "2026-09-23", "close_cny": "97.8", "source_revision": "r-23-corrected", "observed_at": "2026-09-23T22:00:00Z"})
        self.assertNotEqual(first["quote_id"], second["quote_id"])
        rows = self.connection.execute("SELECT * FROM market_index_quotes ORDER BY quote_id").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["supersedes_quote_id"], rows[0]["quote_id"])

    def test_nomination_replay_and_payload_conflict(self) -> None:
        payload = {"nomination_id": "nom-1", "route_id": "fabric-a-b", "shipper_id": "tenant", "service_date": "2026-09-25", "requested_energy_kwh": "80000", "priority": 10, "idempotency_key": "key-1"}
        first = self.service.submit_nomination("dispatch", payload)
        self.assertEqual(first, self.service.submit_nomination("dispatch", payload))
        changed = dict(payload, requested_energy_kwh="81000")
        with self.assertRaises(Conflict):
            self.service.submit_nomination("dispatch", changed)

    def test_outage_reduces_allocation_and_transfer_consumes_inventory(self) -> None:
        self.service.announce_outage("risk", "fabric-a-b", "2026-09-25T00:00:00Z", "2026-09-25T23:59:59Z", "50", "检修")
        for number, requested, priority in ((1, "40000", 10), (2, "30000", 20)):
            self.service.submit_nomination("dispatch", {"nomination_id": f"nom-{number}", "route_id": "fabric-a-b", "shipper_id": f"shipper-{number}", "service_date": "2026-09-25", "requested_energy_kwh": requested, "priority": priority, "idempotency_key": f"key-{number}"})
        allocation = self.service.allocate("dispatch", "fabric-a-b", "2026-09-25")
        self.assertEqual(allocation["available_capacity"], "50000.000")
        self.assertEqual(allocation["allocations"][1]["allocated_energy_kwh"], "10000.000")
        self.service.add_inventory_lot("dispatch", {"lot_id": "lot-1", "facility_id": "cluster-a", "product": "battery-lfp-high", "grade": "PEAK_VALLEY", "quantity_energy_kwh": "60000", "unit_cost_cny": "91", "received_at": "2026-09-24T06:00:00Z"})
        transfer = self.service.dispatch_transfer("dispatch", "transfer-1", "nom-1", "lot-1", 2)
        self.assertEqual(transfer["loaded_energy_kwh"], "40000.000")
        self.assertEqual(self.service.inventory_lot("lot-1")["available_energy_kwh"], "20000.000")

    def test_scenario_is_approved_and_replayed_by_input(self) -> None:
        self.quote(23, "98")
        self.service.add_inventory_lot("dispatch", {"lot_id": "lot-1", "facility_id": "cluster-a", "product": "battery-lfp-high", "grade": "PEAK_VALLEY", "quantity_energy_kwh": "60000", "unit_cost_cny": "91", "received_at": "2026-09-24T06:00:00Z"})
        self.service.create_scenario("plan", {"scenario_id": "restart", "name": "机组检修恢复", "market_index_drop_percent": "9", "route_capacity_changes": {"fabric-a-b": "20"}, "demand_changes": {"cluster-a:battery-lfp-high": "-5"}})
        with self.assertRaises(Forbidden):
            self.service.approve_scenario("plan", "restart", 1)
        self.service.approve_scenario("risk", "restart", 1)
        first = self.service.run_scenario("plan", "restart", "2026-09-23")
        second = self.service.run_scenario("plan", "restart", "2026-09-23")
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["run_id"], second["run_id"])

    def test_audit_chain_detects_tampering(self) -> None:
        self.assertTrue(self.service.audit_chain("audit")["valid"])
        self.connection.execute("UPDATE supply_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("audit")["valid"])

    def test_api_exposes_browser_free_boundary(self) -> None:
        app = JsonApplication(self.service)
        self.assertEqual(app.handle("GET", "/health").status, 200)
        response = app.handle("GET", "/quotes/summary/PEAK_VALLEY", {"X-Actor-Id": "plan"})
        self.assertEqual(response.status, 404)
        self.assertEqual(response.body["error"]["code"], "not_found")


class ConcurrentDispatchTests(unittest.TestCase):
    """两个终端近乎同时确认同一条已分配申请的边界。"""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "dispatch.sqlite3"
        frozen = datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)
        self.service = SupplyService(connect(self.db_path), FrozenClock(frozen))
        for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
            self.service.create_user(user_id, user_id, role)
        self.service.create_facility("plan", {"facility_id": "cluster-a", "name": "北部储能资产平台", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_energy_kwh": "500000"})
        self.service.create_facility("plan", {"facility_id": "pool-b", "name": "东部推理池", "kind": "inference-pool", "timezone": "Asia/Shanghai", "capacity_energy_kwh": "800000"})
        self.service.create_route("plan", {"route_id": "fabric-a-b", "origin_id": "cluster-a", "destination_id": "pool-b", "product": "battery-lfp-high", "daily_capacity": "100000", "loss_basis_points": 25, "transit_hours": 36})
        self.service.add_inventory_lot("dispatch", {"lot_id": "lot-1", "facility_id": "cluster-a", "product": "battery-lfp-high", "grade": "PEAK_VALLEY", "quantity_energy_kwh": "400000", "unit_cost_cny": "91", "received_at": "2026-09-24T06:00:00Z"})

    def _allocate_nomination(self, number: int) -> None:
        self.service.submit_nomination("dispatch", {"nomination_id": f"nom-{number}", "route_id": "fabric-a-b", "shipper_id": f"tenant-{number}", "service_date": f"2026-10-{number:02d}", "requested_energy_kwh": "40000", "priority": 10, "idempotency_key": f"key-{number}"})
        self.service.allocate("dispatch", "fabric-a-b", f"2026-10-{number:02d}")

    def tearDown(self) -> None:
        self.service.connection.close()
        self.tempdir.cleanup()

    def _worker_service(self) -> SupplyService:
        return SupplyService(
            connect(self.db_path),
            FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)),
        )

    def _race(self, left: dict[str, object], right: dict[str, object]) -> tuple[object, object]:
        barrier = threading.Barrier(2)
        outcomes: list[object] = [None, None]

        def run(index: int, request: dict[str, object]) -> None:
            service = self._worker_service()
            barrier.wait()
            try:
                outcomes[index] = ("ok", service.dispatch_transfer("dispatch", **request))
            except Exception as exc:  # 边界必须只暴露业务异常
                outcomes[index] = ("error", type(exc).__name__, str(exc), type(exc).__mro__)
            finally:
                service.connection.close()

        threads = [
            threading.Thread(target=run, args=(0, left)),
            threading.Thread(target=run, args=(1, right)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return outcomes[0], outcomes[1]

    def _assert_single_dispatch_fact(self, transfer_id: str, loaded: str = "40000.000", remaining: str = "360000.000") -> None:
        transfers = self.service.connection.execute("SELECT * FROM transfers ORDER BY transfer_id").fetchall()
        self.assertEqual(len(transfers), 1)
        self.assertEqual(transfers[0]["transfer_id"], transfer_id)
        self.assertEqual(transfers[0]["state"], "in_transit")
        self.assertEqual(transfers[0]["loaded_energy_kwh"], loaded)
        lot = self.service.inventory_lot("lot-1")
        self.assertEqual(lot["available_energy_kwh"], remaining)
        self.assertEqual(lot["revision"], 2)
        dispatch_events = self.service.connection.execute(
            "SELECT count(*) FROM supply_audit_events WHERE event_type='transfer.dispatched'"
        ).fetchone()[0]
        self.assertEqual(dispatch_events, 1)
        verdicts = self.service.connection.execute(
            "SELECT count(*) FROM supply_idempotency WHERE scope='transfer'"
        ).fetchone()[0]
        self.assertEqual(verdicts, 1)
        self.assertTrue(self.service.audit_chain("audit")["valid"])

    def test_equivalent_concurrent_confirms_collapse_to_first_success(self) -> None:
        for number in range(1, 6):  # 同一组并发调用反复验证，裁决始终稳定
            self._allocate_nomination(number)
            request = {"transfer_id": f"dispatch-{number}", "nomination_id": f"nom-{number}", "lot_id": "lot-1", "expected_revision": 2}
            left, right = self._race(dict(request), dict(request))
            self.assertEqual(left[0], "ok", left)
            self.assertEqual(right[0], "ok", right)
            self.assertEqual(left[1], right[1])
            self.assertEqual(right[1]["loaded_energy_kwh"], "40000.000")
            self.assertEqual(right[1]["expected_delivered_energy_kwh"], "39900.000")
            self.assertEqual(right[1]["expected_arrival"], "2026-09-25T20:00:00Z")
        transfers = self.service.connection.execute("SELECT count(*) FROM transfers").fetchone()[0]
        self.assertEqual(transfers, 5)
        self.assertEqual(self.service.inventory_lot("lot-1")["available_energy_kwh"], "200000.000")
        self.assertEqual(
            self.service.connection.execute(
                "SELECT count(*) FROM supply_audit_events WHERE event_type='transfer.dispatched'"
            ).fetchone()[0],
            5,
        )
        self.assertTrue(self.service.audit_chain("audit")["valid"])

    def test_competing_transfer_number_gets_stable_business_conflict(self) -> None:
        self._allocate_nomination(1)
        left, right = self._race(
            {"transfer_id": "transfer-1", "nomination_id": "nom-1", "lot_id": "lot-1", "expected_revision": 2},
            {"transfer_id": "transfer-2", "nomination_id": "nom-1", "lot_id": "lot-1", "expected_revision": 2},
        )
        winner = next(outcome for outcome in (left, right) if outcome[0] == "ok")
        loser = next(outcome for outcome in (left, right) if outcome[0] == "error")
        winning_transfer_id = winner[1]["transfer_id"]
        self.assertIn(winning_transfer_id, {"transfer-1", "transfer-2"})
        self.assertEqual(loser[1], "Conflict", loser)  # 不是 sqlite3.IntegrityError
        self._assert_single_dispatch_fact(winning_transfer_id)

    def test_stale_revision_competitor_gets_business_conflict(self) -> None:
        self._allocate_nomination(1)
        left, right = self._race(
            {"transfer_id": "transfer-1", "nomination_id": "nom-1", "lot_id": "lot-1", "expected_revision": 2},
            {"transfer_id": "transfer-9", "nomination_id": "nom-1", "lot_id": "lot-1", "expected_revision": 1},
        )
        kinds = {outcome[0] for outcome in (left, right)}
        self.assertEqual(kinds, {"ok", "error"})
        failure = next(outcome for outcome in (left, right) if outcome[0] == "error")
        self.assertIn(failure[1], {"Conflict", "InvalidState"})
        self._assert_single_dispatch_fact("transfer-1")

    def test_verdict_survives_restart_and_replays_by_request(self) -> None:
        self._allocate_nomination(1)
        first = self.service.dispatch_transfer("dispatch", "transfer-1", "nom-1", "lot-1", 2)
        self.service.connection.close()
        restarted = SupplyService(
            connect(self.db_path),
            FrozenClock(datetime(2026, 9, 30, 0, 0, tzinfo=timezone.utc)),
        )
        replay = restarted.dispatch_transfer("dispatch", "transfer-1", "nom-1", "lot-1", 2)
        self.assertEqual(replay, first)  # 按请求内容找回原裁决，时钟推进也不改变结果
        self.assertEqual(restarted.inventory_lot("lot-1")["available_energy_kwh"], "360000.000")
        self.assertEqual(
            (
                restarted.connection.execute("SELECT count(*) FROM transfers").fetchone()[0],
                restarted.connection.execute(
                    "SELECT count(*) FROM supply_audit_events WHERE event_type='transfer.dispatched'"
                ).fetchone()[0],
            ),
            (1, 1),
        )
        with self.assertRaises(Conflict):
            restarted.dispatch_transfer("dispatch", "transfer-1", "nom-1", "lot-1", 9)
        restarted.connection.close()

    def test_api_maps_losing_confirm_to_explainable_409(self) -> None:
        self._allocate_nomination(1)
        app = JsonApplication(self.service)
        headers = {"X-Actor-Id": "dispatch", "Content-Type": "application/json"}
        first = app.handle("POST", "/transfers", headers, json.dumps({
            "transfer_id": "transfer-1", "nomination_id": "nom-1", "lot_id": "lot-1", "expected_revision": 2,
        }).encode("utf-8"))
        self.assertEqual(first.status, 201)
        loser = app.handle("POST", "/transfers", headers, json.dumps({
            "transfer_id": "transfer-2", "nomination_id": "nom-1", "lot_id": "lot-1", "expected_revision": 2,
        }).encode("utf-8"))
        self.assertEqual(loser.status, 409)
        self.assertEqual(loser.body["error"]["code"], "conflict")
        self.assertNotIn("UNIQUE", loser.body["error"]["message"])
        self.assertNotIn("sqlite", loser.body["error"]["message"].lower())


if __name__ == "__main__":
    unittest.main()
