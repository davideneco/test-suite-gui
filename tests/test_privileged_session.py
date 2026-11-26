"""Tests unitaires pour la session privilégiée persistante (core/priv_helper.py, core/privileged.py, core/fio_runner.py)."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import fio_runner, privileged


class TestPrivHelperProtocol(unittest.TestCase):
    """Vérifie le protocole JSON de core/priv_helper.py sur de vrais pipes stdin/stdout."""

    def setUp(self):
        privileged.close_elevated_session()

    def tearDown(self):
        privileged.close_elevated_session()

    def test_helper_run_start_kill_and_shutdown(self):
        helper_path = os.path.join(ROOT, "core", "priv_helper.py")
        client, err = privileged._PrivHelperClient.launch([sys.executable, "-u", helper_path], auth_timeout_s=10.0)
        self.assertIsNotNone(client, f"Échec lancement helper : {err}")
        try:
            self.assertTrue(client.is_alive())

            # 1. Commande synchrone réussie
            r1 = client.run([sys.executable, "-c", "import sys; print('ok-out'); print('ok-err', file=sys.stderr)"])
            self.assertEqual(r1.returncode, 0)
            self.assertIn("ok-out", r1.stdout)
            self.assertIn("ok-err", r1.stderr)

            # 2. Timeout synchrone lève TimeoutExpired et tue le fils
            with self.assertRaises(subprocess.TimeoutExpired):
                client.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.3)

            # 3. Job asynchrone long + arrêt explicite
            job = client.start_job([sys.executable, "-c", "import time; time.sleep(30)"])
            self.assertIsNone(job.poll())
            self.assertGreater(job.pid, 0)
            self.assertTrue(job.stop_elevated("TERM"))
            rc = job.wait(timeout=5)
            self.assertNotEqual(rc, 0)
            self.assertIsNotNone(job.poll())

            # 4. Job asynchrone normal + sortie récupérée
            job2 = client.start_job([sys.executable, "-c", "print('hello-job')"])
            rc2 = job2.wait(timeout=5)
            self.assertEqual(rc2, 0)
            out2, _ = job2.get_output()
            self.assertIn("hello-job", out2)
        finally:
            client.close()
            self.assertFalse(client.is_alive())


class TestPrivilegedSingleSession(unittest.TestCase):
    """Vérifie que `pkexec` n'est appelé qu'une seule fois par session et jamais en passif."""

    def setUp(self):
        privileged.close_elevated_session()

    def tearDown(self):
        privileged.close_elevated_session()

    def test_run_if_elevated_never_prompts_when_inactive(self):
        with mock.patch.object(privileged, "is_root", return_value=False), \
             mock.patch.object(privileged, "has_sudo_nopasswd", return_value=False), \
             mock.patch.object(privileged, "ensure_elevated_session") as m_ens:
            res = privileged.run_if_elevated(["nvme", "smart-log", "/dev/nvme0", "-o", "json"])
            self.assertIsNone(res)
            m_ens.assert_not_called()

    def test_single_pkexec_launch_reused_across_multiple_calls_and_threads(self):
        helper_path = os.path.join(ROOT, "core", "priv_helper.py")
        launch_count = 0
        launch_lock = threading.Lock()
        real_launch = privileged._PrivHelperClient.launch

        def fake_select():
            return "pkexec", [sys.executable, "-u", helper_path]

        def counting_launch(cmd, auth_timeout_s=120.0):
            nonlocal launch_count
            with launch_lock:
                launch_count += 1
            return real_launch(cmd, auth_timeout_s=auth_timeout_s)

        with mock.patch.object(privileged, "is_root", return_value=False), \
             mock.patch.object(privileged, "has_sudo_nopasswd", return_value=False), \
             mock.patch.object(privileged, "_select_launcher", side_effect=fake_select), \
             mock.patch.object(privileged._PrivHelperClient, "launch", side_effect=counting_launch):

            # Appels concurrents : 1 seul assistant doit être démarré
            results = []
            def worker(idx):
                r = privileged.run_elevated([sys.executable, "-c", f"print({idx})"], timeout=10)
                results.append((idx, r.returncode, r.stdout.strip()))

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=15)

            self.assertEqual(len(results), 4)
            for idx, rc, out in results:
                self.assertEqual(rc, 0)
                self.assertEqual(out, str(idx))

            # Appel passif `run_if_elevated` réutilise la session sans relancer pkexec
            r_passive = privileged.run_if_elevated([sys.executable, "-c", "print('smart-ok')"], timeout=5)
            self.assertIsNotNone(r_passive)
            self.assertEqual(r_passive.returncode, 0)
            self.assertIn("smart-ok", r_passive.stdout)

            # Démarrage de job long réutilise aussi la session
            job = privileged.start_elevated_job([sys.executable, "-c", "print('fio-ok')"])
            self.assertEqual(job.wait(timeout=5), 0)
            self.assertEqual(launch_count, 1)

    def test_cancelled_polkit_returns_126_immediately(self):
        with mock.patch.object(privileged, "is_root", return_value=False), \
             mock.patch.object(privileged, "has_sudo_nopasswd", return_value=False), \
             mock.patch.object(privileged, "_select_launcher",
                               return_value=("pkexec", [sys.executable, "-c", "import sys; sys.stderr.write('Request dismissed\\n'); sys.exit(126)"])):
            t0 = time.monotonic()
            r = privileged.run_elevated(["fio", "--version"], timeout=10)
            elapsed = time.monotonic() - t0
            self.assertEqual(r.returncode, 126)
            self.assertLess(elapsed, 3.0)
            self.assertIn("dismissed", r.stderr.lower())


class TestFioSuiteSingleAuth(unittest.TestCase):
    """Simule une suite fio multi-étapes sur /dev/nvme1n1 avec un faux fio : 1 seule autorisation."""

    def setUp(self):
        privileged.close_elevated_session()
        self.tmp = tempfile.mkdtemp(prefix="benchsuite_fio_auth_")
        self.fake_fio = os.path.join(self.tmp, "fio")
        with open(self.fake_fio, "w", encoding="utf-8") as fh:
            fh.write(
                f"#!{sys.executable}\n"
                "import json, sys, time\n"
                "if '--slow' in sys.argv:\n"
                "    time.sleep(30)\n"
                "payload = {'jobs': [{'jobname': 'job0', 'job_options': {'filename': '/dev/nvme1n1'}, "
                "'read': {'bw_bytes': 5000000000, 'iops': 100000, 'lat_ns': {'mean': 50000, 'max': 200000}, 'clat_ns': {'percentile': {'99.000000': 100000}}}, "
                "'write': {'bw_bytes': 4000000000, 'iops': 80000, 'lat_ns': {'mean': 60000, 'max': 250000}, 'clat_ns': {'percentile': {'99.000000': 120000}}}}]}\n"
                "print(json.dumps(payload))\n"
            )
        os.chmod(self.fake_fio, 0o755)

    def tearDown(self):
        privileged.close_elevated_session()
        try:
            os.remove(self.fake_fio)
            os.rmdir(self.tmp)
        except Exception:
            pass

    def test_fio_suite_runs_all_steps_with_one_helper_launch(self):
        helper_path = os.path.join(ROOT, "core", "priv_helper.py")
        launch_count = 0
        real_launch = privileged._PrivHelperClient.launch

        def counting_launch(cmd, auth_timeout_s=120.0):
            nonlocal launch_count
            launch_count += 1
            return real_launch(cmd, auth_timeout_s=auth_timeout_s)

        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = self.tmp + os.pathsep + old_path
        try:
            with mock.patch.object(os, "geteuid", return_value=1000), \
                 mock.patch.object(privileged, "is_root", return_value=False), \
                 mock.patch.object(privileged, "has_sudo_nopasswd", return_value=False), \
                 mock.patch.object(privileged, "_select_launcher", return_value=("pkexec", [sys.executable, "-u", helper_path])), \
                 mock.patch.object(privileged._PrivHelperClient, "launch", side_effect=counting_launch), \
                 mock.patch("core.safety.check_targets", return_value=("ok", "")), \
                 mock.patch("core.fault_monitor.with_fault_watch", lambda f: f), \
                 mock.patch("core.ssd_diagnostics.read_ssd_sensors", return_value=(40.0, 5.0, [])):

                # Exécute 3 étapes de suite sur /dev/nvme1n1
                res = fio_runner.run_fio_suite(
                    ["peak_seq1m_q8t1_read", "peak_seq1m_q8t1_write", "peak_rnd4k_q32t16_read"],
                    path="/dev/nvme1n1",
                    raw_device=True,
                    suite_name="Test Auth Unique",
                )
                self.assertEqual(res.get("status"), "ok", f"Erreur suite : {res}")
                self.assertEqual(len(res.get("suite_results", [])), 3)
                self.assertEqual(launch_count, 1)

                # Un arrêt manuel d'un second test ne relance pas l'assistant non plus
                stop_flag = {"v": False}
                def trigger_stop():
                    time.sleep(0.3)
                    stop_flag["v"] = True
                threading.Thread(target=trigger_stop, daemon=True).start()
                rc, _, err = fio_runner._run_fio_popen(
                    ["fio", "--filename=/dev/nvme1n1", "--slow"],
                    timeout=30,
                    stop_flag_fn=lambda: stop_flag["v"],
                    target_path="/dev/nvme1n1",
                )
                self.assertEqual(rc, -99)
                self.assertEqual(launch_count, 1)
        finally:
            os.environ["PATH"] = old_path


if __name__ == "__main__":
    unittest.main()
