from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

import httpx

from terminal_app.paths import OctoPaths
from terminal_app.provider_config import (
    OpenAICompatibleProviderConfig,
    ProviderSettings,
    save_provider_settings,
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class OctoWebCliProcessTests(unittest.TestCase):
    def test_octo_web_process_serves_static_build_and_enforces_boundaries(self) -> None:
        root = Path(__file__).resolve().parents[2]
        static_dir = root / "web" / "out"
        self.assertTrue((static_dir / "index.html").is_file(), "请先构建 web/out")

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            home = base / "home"
            cwd = base / "workspace"
            home.mkdir(parents=True)
            cwd.mkdir(parents=True)
            (home / "settings.json").write_text('{"memory":{"enabled":false}}', encoding="utf-8")
            save_provider_settings(
                ProviderSettings(
                    default_provider="deepseek",
                    providers=(
                        OpenAICompatibleProviderConfig(
                            name="deepseek",
                            base_url="https://example.invalid/v1",
                            api="openai-completions",
                            api_key_env="OCTO_PROCESS_DUMMY_KEY",
                            models=("deepseek-flash",),
                            default_model="deepseek-flash",
                        ),
                    ),
                ),
                OctoPaths(home=home, agents_home=base / "agents"),
            )

            port = _free_port()
            origin = f"http://127.0.0.1:{port}"
            env = {
                **os.environ,
                "OCTO_HOME": str(home),
                "OCTO_OFFLINE": "1",
                "OCTO_PROCESS_DUMMY_KEY": "sk-dummy-process-secret",
            }
            command = [
                sys.executable,
                "-m",
                "web_api.cli",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--cwd",
                str(cwd),
                "--static-dir",
                str(static_dir),
            ]
            proc = subprocess.Popen(
                command,
                cwd=str(cwd),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                deadline = time.monotonic() + 20.0
                health_data = None
                with httpx.Client(base_url=origin, trust_env=False, timeout=1.0) as client:
                    while time.monotonic() < deadline:
                        if proc.poll() is not None:
                            output = proc.stdout.read() if proc.stdout else ""
                            self.fail(f"octo-web 提前退出 ({proc.returncode}):\n{output}")
                        try:
                            resp = client.get("/api/health")
                            if resp.status_code == 200:
                                health_data = resp.json()
                                break
                        except httpx.HTTPError:
                            time.sleep(0.1)
                    if health_data is None:
                        proc.terminate()
                        output = proc.stdout.read() if proc.stdout else ""
                        self.fail(f"octo-web 未在 20s 内就绪:\n{output}")
                    assert health_data is not None
                    self.assertTrue(health_data["ok"])

                    page = client.get("/")
                    self.assertEqual(page.status_code, 200)
                    self.assertIn("Octo", page.text)
                    self.assertNotIn("sk-dummy-process-secret", page.text)

                    state = client.get("/api/state")
                    self.assertEqual(state.status_code, 200)
                    self.assertEqual(state.json()["model"], "deepseek-flash")

                    snap = client.get("/api/snapshot")
                    self.assertEqual(snap.status_code, 200)
                    self.assertEqual(snap.json()["selected_topic_id"], "main")

                    db_probe = client.get("/../home/octo.db")
                    self.assertEqual(db_probe.status_code, 404)

                    unknown_api = client.get("/api/unknown")
                    self.assertEqual(unknown_api.status_code, 404)
                    self.assertEqual(unknown_api.headers["content-type"].split(";")[0], "application/json")
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
                if proc.stdout:
                    proc.stdout.close()


if __name__ == "__main__":
    unittest.main()
