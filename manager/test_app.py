"""Tests for manager/app.py - currently just /power-off, the one route
with real side effects worth pinning down (shelling out to `sudo
shutdown`). Same shape as GR6-v2's manager/test_app.py, minus the drive
cutoff.
"""

import unittest
from unittest.mock import patch

import app


class PowerOffTestCase(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()

    def test_shells_out_to_shutdown(self):
        with patch.object(app.subprocess, "run") as run:
            run.return_value.returncode = 0
            run.return_value.stderr = ""
            resp = self.client.post("/power-off")
        self.assertEqual(resp.status_code, 204)
        run.assert_called_once_with(["sudo", "shutdown", "-h", "now"], capture_output=True, text=True)

    def test_get_does_nothing(self):
        with patch.object(app.subprocess, "run") as run:
            resp = self.client.get("/power-off")
        self.assertEqual(resp.status_code, 405)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
