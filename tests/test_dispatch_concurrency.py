"""发车确认的并发裁决边界：等价重试回放、竞争冲突、版本过期与重启找回。"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from battery_logistics.clock import FrozenClock
from battery_logistics.errors import Conflict
from battery_logistics.service import SupplyService
from battery_logistics.storage import connect


class DispatchConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = connect(":memory:")
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = SupplyService(self.connection, self.clock)
        for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("audit", "auditor")):
            self.service.create_user(user_id, user_id, role)
        self.service.create_facility("plan", {"facility_id": "cluster-a", "name": "北部储能资产平台", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_energy_kwh": "500000"})
        self.service.create_facility("plan", {"facility_id": "pool-b", "name": "东部推理池", "kind": "inference-pool", "timezone": "Asia/Shanghai", "capacity_energy_kwh": "800000"})
        self.service.create_route("plan", {"route_id": "fabric-a-b", "origin_id": "cluster-a", "destination_id": "pool-b", "product": "battery-lfp-high", "daily_capacity": "1000000", "loss_basis_points": 25, "transit_hours": 36})
        self.service.add_inventory_lot("dispatch", {"lot_id": "lot-1", "facility_id": "cluster-a", "product": "battery-lfp-high", "grade": "PEAK_VALLEY", "quantity_energy_kwh": "1000000", "unit_cost_cny": "91", "received_at": "2026-09-24T06:00:00Z"})

    def tearDown(self) -> None:
        self.connection.close()

    def _allocated_nomination(self, number: int, requested: str = "40000") -> None:
        self.service.submit_nomination("dispatch", {"nomination_id": f"nom-{number}", "route_id": "fabric-a-b", "shipper_id": f"shipper-{number}", "service_date": "2026-09-25", "requested_energy_kwh": requested, "priority": number, "idempotency_key": f"key-{number}"})
        self.service.allocate("dispatch", "fabric-a-b", "2026-09-25")

    def _dispatch_facts(self) -> tuple[int, int, int, int]:
        transfers = self.connection.execute("SELECT count(*) FROM transfers").fetchone()[0]
        audits = self.connection.execute("SELECT count(*) FROM supply_audit_events WHERE event_type='transfer.dispatched'").fetchone()[0]
        available = self.connection.execute("SELECT available_energy_kwh FROM inventory_lots WHERE lot_id='lot-1'").fetchone()[0]
        revision = self.connection.execute("SELECT revision FROM inventory_lots WHERE lot_id='lot-1'").fetchone()[0]
        return transfers, audits, int(float(available)), revision

    def _concurrent_pair(self, left_kwargs: dict, right_kwargs: dict) -> tuple[dict, object, dict, object]:
        barrier = threading.Barrier(2)
        results: dict[str, object] = {}

        def worker(side: str, kwargs: dict) -> None:
            barrier.wait()
            try:
                results[side] = self.service.dispatch_transfer("dispatch", **kwargs)
            except BaseException as exc:  # noqa: BLE001 - 并发裁决结果需要回传
                results[side + "_error"] = exc

        t1 = threading.Thread(target=worker, args=("left", left_kwargs))
        t2 = threading.Thread(target=worker, args=("right", right_kwargs))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        return results.get("left"), results.get("left_error"), results.get("right"), results.get("right_error")

    def test_equivalent_concurrent_confirmations_replay_first_verdict(self) -> None:
        # 反复用同一组并发调用验证，每轮都必须是一个成功事实 + 一个可解释回放。
        for round_no in range(1, 6):
            self._allocated_nomination(round_no)
            kwargs = {"transfer_id": f"transfer-{round_no}", "nomination_id": f"nom-{round_no}", "lot_id": "lot-1", "expected_revision": 2}
            left, left_err, right, right_err = self._concurrent_pair(dict(kwargs), dict(kwargs))
            self.assertIsNone(left_err, left_err)
            self.assertIsNone(right_err, right_err)
            payloads = [left, right]
            replays = sorted(bool(item["replayed"]) for item in payloads)
            self.assertEqual(replays, [False, True])
            self.assertEqual({item["transfer_id"] for item in payloads}, {f"transfer-{round_no}"})
            for item in payloads:
                self.assertEqual(item["state"], "in_transit")
                self.assertEqual(item["loaded_energy_kwh"], "40000.000")
                self.assertEqual(item["expected_delivered_energy_kwh"], "39900.000")
                self.assertEqual(item["expected_arrival"], "2026-09-25T20:00:00Z")
            transfers, audits, available, revision = self._dispatch_facts()
            self.assertEqual(transfers, round_no)
            self.assertEqual(audits, round_no)
            self.assertEqual(available, 1_000_000 - 40_000 * round_no)
            self.assertEqual(revision, 1 + round_no)
        # 审计链不允许被失败一方污染。
        self.assertTrue(self.service.audit_chain("audit")["valid"])

    def test_different_transfer_ids_produce_one_fact_and_one_conflict(self) -> None:
        self._allocated_nomination(1)
        left, left_err, right, right_err = self._concurrent_pair(
            {"transfer_id": "transfer-a", "nomination_id": "nom-1", "lot_id": "lot-1", "expected_revision": 2},
            {"transfer_id": "transfer-b", "nomination_id": "nom-1", "lot_id": "lot-1", "expected_revision": 2},
        )
        winner = left or right
        loser_error = left_err or right_err
        self.assertIsNotNone(winner)
        self.assertIsInstance(loser_error, Conflict)
        self.assertNotIn("UNIQUE", str(loser_error))
        self.assertNotIn("sqlite", str(loser_error).lower())
        self.assertIn(winner["transfer_id"], {"transfer-a", "transfer-b"})  # 只有一方落库
        transfers, audits, available, revision = self._dispatch_facts()
        self.assertEqual((transfers, audits, available, revision), (1, 1, 960_000, 2))
        # 失败方再用相同（错误）参数重试，结论必须稳定。
        loser_id = "transfer-b" if winner["transfer_id"] == "transfer-a" else "transfer-a"
        with self.assertRaises(Conflict):
            self.service.dispatch_transfer("dispatch", loser_id, "nom-1", "lot-1", 2)
        self.assertEqual(self._dispatch_facts(), (1, 1, 960_000, 2))

    def test_stale_revision_competing_request_gets_business_conflict(self) -> None:
        self._allocated_nomination(1)
        left, left_err, right, right_err = self._concurrent_pair(
            {"transfer_id": "transfer-1", "nomination_id": "nom-1", "lot_id": "lot-1", "expected_revision": 2},
            {"transfer_id": "transfer-1", "nomination_id": "nom-1", "lot_id": "lot-1", "expected_revision": 1},
        )
        winner = left or right
        stale_error = left_err or right_err
        self.assertIsNotNone(winner)
        self.assertIsInstance(stale_error, Conflict)
        self.assertEqual(self._dispatch_facts(), (1, 1, 960_000, 2))
        # 胜出事实之后用过期版本再确认仍是稳定业务冲突。
        with self.assertRaises(Conflict):
            self.service.dispatch_transfer("dispatch", "transfer-1", "nom-1", "lot-1", 1)

    def test_verdict_survives_restart_and_replays_by_request_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dispatch.sqlite3"
            connection = connect(path)
            service = SupplyService(connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))
            for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("audit", "auditor")):
                service.create_user(user_id, user_id, role)
            service.create_facility("plan", {"facility_id": "cluster-a", "name": "北部储能资产平台", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_energy_kwh": "500000"})
            service.create_facility("plan", {"facility_id": "pool-b", "name": "东部推理池", "kind": "inference-pool", "timezone": "Asia/Shanghai", "capacity_energy_kwh": "800000"})
            service.create_route("plan", {"route_id": "fabric-a-b", "origin_id": "cluster-a", "destination_id": "pool-b", "product": "battery-lfp-high", "daily_capacity": "1000000", "loss_basis_points": 25, "transit_hours": 36})
            service.add_inventory_lot("dispatch", {"lot_id": "lot-1", "facility_id": "cluster-a", "product": "battery-lfp-high", "grade": "PEAK_VALLEY", "quantity_energy_kwh": "1000000", "unit_cost_cny": "91", "received_at": "2026-09-24T06:00:00Z"})
            self._allocated_nomination_on(service, 1)
            first = service.dispatch_transfer("dispatch", "transfer-1", "nom-1", "lot-1", 2)
            self.assertFalse(first["replayed"])
            connection.close()

            # 模拟进程重启：凭同一组请求内容找回原裁决。
            restarted = connect(path)
            recovered_service = SupplyService(restarted)
            replay = recovered_service.dispatch_transfer("dispatch", "transfer-1", "nom-1", "lot-1", 2)
            self.assertTrue(replay["replayed"])
            self.assertEqual(replay["transfer_id"], first["transfer_id"])
            self.assertEqual(replay["loaded_energy_kwh"], first["loaded_energy_kwh"])
            self.assertEqual(replay["expected_delivered_energy_kwh"], first["expected_delivered_energy_kwh"])
            self.assertEqual(replay["expected_arrival"], "2026-09-25T20:00:00Z")
            self.assertEqual(restarted.execute("SELECT count(*) FROM transfers").fetchone()[0], 1)
            self.assertEqual(restarted.execute("SELECT available_energy_kwh FROM inventory_lots WHERE lot_id='lot-1'").fetchone()[0], "960000.000")
            with self.assertRaises(Conflict):
                recovered_service.dispatch_transfer("dispatch", "transfer-other", "nom-1", "lot-1", 2)
            self.assertTrue(recovered_service.audit_chain("audit")["valid"])
            restarted.close()

    @staticmethod
    def _allocated_nomination_on(service: SupplyService, number: int) -> None:
        service.submit_nomination("dispatch", {"nomination_id": f"nom-{number}", "route_id": "fabric-a-b", "shipper_id": f"shipper-{number}", "service_date": "2026-09-25", "requested_energy_kwh": "40000", "priority": number, "idempotency_key": f"key-{number}"})
        service.allocate("dispatch", "fabric-a-b", "2026-09-25")

    def test_two_connections_contend_at_storage_layer(self) -> None:
        # 两个进程/连接各自持有服务实例，在 SQLite 写锁层面真实竞争。
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dispatch-contend.sqlite3"
            seed = connect(path)
            seed_service = SupplyService(seed, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))
            for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("audit", "auditor")):
                seed_service.create_user(user_id, user_id, role)
            seed_service.create_facility("plan", {"facility_id": "cluster-a", "name": "北部储能资产平台", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_energy_kwh": "500000"})
            seed_service.create_facility("plan", {"facility_id": "pool-b", "name": "东部推理池", "kind": "inference-pool", "timezone": "Asia/Shanghai", "capacity_energy_kwh": "800000"})
            seed_service.create_route("plan", {"route_id": "fabric-a-b", "origin_id": "cluster-a", "destination_id": "pool-b", "product": "battery-lfp-high", "daily_capacity": "1000000", "loss_basis_points": 25, "transit_hours": 36})
            seed_service.add_inventory_lot("dispatch", {"lot_id": "lot-1", "facility_id": "cluster-a", "product": "battery-lfp-high", "grade": "PEAK_VALLEY", "quantity_energy_kwh": "1000000", "unit_cost_cny": "91", "received_at": "2026-09-24T06:00:00Z"})
            self._allocated_nomination_on(seed_service, 1)
            seed.close()

            conn_a = connect(path)
            conn_b = connect(path)
            service_a = SupplyService(conn_a)
            service_b = SupplyService(conn_b)
            outcomes: dict[str, object] = {}
            barrier = threading.Barrier(2)

            def confirm(name: str, service: SupplyService) -> None:
                barrier.wait()
                try:
                    outcomes[name] = service.dispatch_transfer("dispatch", "transfer-1", "nom-1", "lot-1", 2)
                except BaseException as exc:  # noqa: BLE001
                    outcomes[name + "_error"] = exc

            t1 = threading.Thread(target=confirm, args=("a", service_a))
            t2 = threading.Thread(target=confirm, args=("b", service_b))
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            payloads = [outcomes.get("a"), outcomes.get("b")]
            present = [item for item in payloads if item is not None]
            self.assertEqual(len(present), 2)
            self.assertEqual(sorted(bool(item["replayed"]) for item in present), [False, True])
            self.assertNotIn("a_error", outcomes)
            self.assertNotIn("b_error", outcomes)
            self.assertEqual(conn_a.execute("SELECT count(*) FROM transfers").fetchone()[0], 1)
            self.assertEqual(conn_b.execute("SELECT count(*) FROM transfers").fetchone()[0], 1)
            self.assertEqual(conn_a.execute("SELECT available_energy_kwh FROM inventory_lots WHERE lot_id='lot-1'").fetchone()[0], "960000.000")
            self.assertEqual(conn_a.execute("SELECT count(*) FROM supply_audit_events WHERE event_type='transfer.dispatched'").fetchone()[0], 1)
            conn_a.close()
            conn_b.close()


if __name__ == "__main__":
    unittest.main()
