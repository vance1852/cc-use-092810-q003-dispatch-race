"""容器和快照检查使用的冒烟验收命令。"""

from __future__ import annotations

import argparse
import json

from .service import ComponentQualityService


def run() -> dict:
    service = ComponentQualityService()
    service.bootstrap_admin()
    token = service.auth.login("admin", "component-admin")
    service.create_lot(token, "LOT-DEMO", "CMOS image sensor", "P3.2", 10)
    for test_frequency, response in ((450, .71), (520, .93), (650, .84)):
        service.add_measurement(token, "LOT-DEMO", test_frequency, response, .01, "spectrometer-1")
    result = service.analyze(token, "LOT-DEMO")
    service.approve(token, "LOT-DEMO", "hold", "awaiting quality review")
    return {"status": "ok", "lot": result["lot_id"], "peak": result["response_profile"]["peak_test_frequency_hz"], "events": len(service.audit(token, "LOT-DEMO"))}


def main() -> None:
    argparse.ArgumentParser().parse_args()
    print(json.dumps(run(), ensure_ascii=False))


if __name__ == "__main__":
    main()
