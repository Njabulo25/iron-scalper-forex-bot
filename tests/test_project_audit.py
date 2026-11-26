"""Offline regression checks for the project audit fixes.

Run: .venv\\Scripts\\python.exe tests\\test_project_audit.py
"""

import io
import re
import sys
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from werkzeug.datastructures import FileStorage

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.engine.credentials import hash_passphrase, verify_passphrase
from src.strategies.risk_manager import RiskManager


class CredentialTests(unittest.TestCase):
    def test_passphrases_are_salted_and_verified(self):
        passphrase = "a strong test passphrase"
        first = hash_passphrase(passphrase)
        second = hash_passphrase(passphrase)
        self.assertNotEqual(first, second)
        self.assertTrue(verify_passphrase(passphrase, first))
        self.assertFalse(verify_passphrase("incorrect passphrase", first))


class RiskTests(unittest.TestCase):
    def test_minimum_volume_cannot_exceed_risk_budget(self):
        risk = RiskManager(1000.0, risk_per_trade=0.01)
        symbol = SimpleNamespace(volume_min=0.01, volume_max=1.0, volume_step=0.01)
        self.assertEqual(
            risk.size_position(1000.0, 2000.0, 1995.0, symbol, loss_per_lot=1001.0),
            0.0,
        )
        self.assertEqual(
            risk.size_position(1000.0, 2000.0, 1995.0, symbol, loss_per_lot=510.0),
            0.01,
        )

    def test_daily_state_resets_using_broker_day(self):
        today = date(2026, 11, 25)
        risk = RiskManager(1000.0)
        risk.register_open(today=today)
        risk.register_close(-20.0, today=today)
        allowed, _ = risk.can_trade(today=today + timedelta(days=1))
        self.assertTrue(allowed)
        self.assertEqual(risk.daily_trades, 0)
        self.assertEqual(risk.daily_pnl, 0.0)
        self.assertEqual(risk.day_start_balance, 980.0)


class ChartUploadTests(unittest.TestCase):
    def test_analysis_does_not_claim_price_direction(self):
        from src.engine import graph_analyzer

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "chart.png"
            Image.new("RGB", (32, 24), (80, 120, 160)).save(path)
            result = graph_analyzer.analyze_chart(path)
        self.assertEqual(result["price_trend"], "NOT ANALYZED")
        self.assertEqual(result["image_size"], {"width": 32, "height": 24})
        self.assertIn("does not identify", result["analysis_note"])

    def test_upload_checks_real_image_content_before_saving(self):
        from src.engine import graph_analyzer

        with tempfile.TemporaryDirectory() as folder:
            original = graph_analyzer._upload_directory
            graph_analyzer._upload_directory = Path(folder)
            try:
                invalid = FileStorage(stream=io.BytesIO(b"not an image"), filename="chart.png")
                with self.assertRaises(ValueError):
                    graph_analyzer.save_upload(invalid)
                valid_stream = io.BytesIO()
                Image.new("RGB", (8, 8), (10, 20, 30)).save(valid_stream, format="PNG")
                valid_stream.seek(0)
                valid = FileStorage(stream=valid_stream, filename="chart.png")
                saved = Path(graph_analyzer.save_upload(valid))
                self.assertTrue(saved.is_file())
            finally:
                graph_analyzer._upload_directory = original


class DashboardAccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from src.engine import database
        from src.engine import web_dashboard_next

        cls.database = database
        cls.dashboard = web_dashboard_next
        cls.dashboard.app.config.update(TESTING=True, SECRET_KEY="audit-test-secret")

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.original_db_path = self.database.DATABASE_PATH
        self.database.DATABASE_PATH = Path(self.folder.name) / "audit.db"
        self.database._INITIALIZED.clear()
        self.database.init_db()
        self.database.add_user("operator", "operator test passphrase", "admin")
        self.database.add_user("viewer", "viewer test passphrase", "viewer")
        self.dashboard.trader_instance = None

    def tearDown(self):
        with self.dashboard.state_lock:
            self.dashboard.state.update({
                "running": False, "status": "Stopped",
                "loop_heartbeat_epoch": 0, "loop_duration_ms": None,
            })
        self.database.DATABASE_PATH = self.original_db_path
        self.database._INITIALIZED.clear()
        self.folder.cleanup()

    def _sign_in(self, username, passphrase):
        client = self.dashboard.app.test_client()
        response = client.get("/login")
        token = re.search(rb'<meta name="csrf-token" content="([^"]+)"', response.data).group(1).decode()
        login = client.post("/login", data={
            "username": username,
            "passphrase": passphrase,
            "_csrf_token": token,
        })
        self.assertEqual(login.status_code, 302)
        page = client.get("/dashboard")
        next_token = re.search(rb'<meta name="csrf-token" content="([^"]+)"', page.data).group(1).decode()
        return client, next_token, page

    def test_landing_page_and_control_authorization(self):
        landing = self.dashboard.app.test_client().get("/")
        self.assertEqual(landing.status_code, 200)
        self.assertIn(b"Signal to execution", landing.data)
        self.assertIn(b'href="/static/css/landing.css"', landing.data)
        landing_style = self.dashboard.app.test_client().get("/static/css/landing.css")
        self.assertEqual(landing_style.status_code, 200)
        landing_style.close()
        landing.close()

        viewer, viewer_token, viewer_page = self._sign_in("viewer", "viewer test passphrase")
        self.assertNotIn(b'id="btn-start"', viewer_page.data)
        self.assertNotIn(b'id="bt-run"', viewer_page.data)
        self.assertIn(b"Read-only viewer", viewer_page.data)
        self.assertEqual(viewer.get("/").status_code, 200)
        denied = viewer.post("/stop", headers={"X-CSRF-Token": viewer_token})
        self.assertEqual(denied.status_code, 403)
        denied_backtest = viewer.post("/api/backtests/run", json={}, headers={"X-CSRF-Token": viewer_token})
        self.assertEqual(denied_backtest.status_code, 403)
        denied_upload = viewer.post("/upload_graph", headers={"X-CSRF-Token": viewer_token})
        self.assertEqual(denied_upload.status_code, 403)

        admin, admin_token, admin_page = self._sign_in("operator", "operator test passphrase")
        self.assertIn(b'id="btn-start"', admin_page.data)
        self.assertIn(b"EMA scalper on M1 bars", admin_page.data)
        self.assertEqual(admin.get("/landing").status_code, 200)
        self.assertEqual(admin.get("/login").status_code, 302)
        for asset in ("/static/css/app.css", "/static/js/app.js", "/static/js/charts.js", "/static/img/iron-scalper-mark.svg"):
            response = admin.get(asset)
            self.assertEqual(response.status_code, 200, asset)
            response.close()
        configured = admin.get("/api/config").get_json()["config"]
        self.assertEqual(configured["scalper"]["timeframe"], "M1")
        self.assertEqual(configured["breakout"]["timeframe"], "M5")
        self.assertEqual(configured["ml"]["timeframe"], "M1")
        self.assertEqual(admin.get("/api/ml").get_json()["configured"]["timeframe"], "M1")
        for path in (
            "/state", "/api/account", "/api/positions", "/api/health", "/api/logs",
            "/api/performance", "/api/analytics/daily", "/api/analytics/weekly",
            "/api/analytics/strategies", "/api/analytics/sessions", "/api/analytics/equity",
            "/api/analytics/monthly", "/api/trades", "/api/models", "/api/backtests",
        ):
            response = admin.get(path)
            self.assertEqual(response.status_code, 200, path)
            response.close()
        missing_csrf = admin.post("/stop")
        self.assertEqual(missing_csrf.status_code, 400)
        stopped = admin.post("/stop", headers={"X-CSRF-Token": admin_token})
        self.assertEqual(stopped.status_code, 200)
        health = admin.get("/api/health").get_json()
        self.assertEqual(health["engine"]["loop_heartbeat_status"], "stopped")
        with self.dashboard.state_lock:
            self.dashboard.state.update({
                "running": True,
                "status": "Running",
                "loop_heartbeat_epoch": time.time() - 120,
                "config": {"execution": {"loop_seconds": 2.0}},
            })
        health = admin.get("/api/health").get_json()
        self.assertEqual(health["engine"]["loop_heartbeat_status"], "stale")


if __name__ == "__main__":
    unittest.main(verbosity=2)
