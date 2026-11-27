# -*- coding: utf-8 -*-
"""Tests des profils de séquences automatiques (CPU+RAM+GPU+SSD, CPU+RAM+GPU, CPU+RAM+SSD)."""

import unittest

from core import campaign as cp
from core.runner import list_suites, load_suite, parallel_conflicts, validate_steps


class TestSequenceProfiles(unittest.TestCase):
    def test_shipped_profile_suites_exist_and_validate(self):
        ids = {s["id"] for s in list_suites()}
        for sid in ("cpu_ram_gpu_ssd", "cpu_ram_gpu", "cpu_ram_ssd"):
            self.assertIn(sid, ids)
            suite = load_suite(sid)
            errs = validate_steps(suite["steps"])
            self.assertEqual(errs, [], msg=f"Suite {sid} invalide : {errs}")

    def test_select_gpu_tools_fallback_chain(self):
        # NVIDIA complet
        nv_full = cp.select_gpu_tools(
            tool_check=lambda t: t in ("gpu-fryer", "gpu-burn", "nvbandwidth"),
            has_nvidia=True,
        )
        self.assertEqual(nv_full["stress"]["id"], "gpu_fryer")
        self.assertEqual(nv_full["vram"]["id"], "gpu_vram_test")
        self.assertEqual(nv_full["perf"]["id"], "gpu_nvbandwidth")

        # NVIDIA sans gpu-fryer -> repli gpu-burn
        nv_burn = cp.select_gpu_tools(
            tool_check=lambda t: t in ("gpu-burn",),
            has_nvidia=True,
        )
        self.assertEqual(nv_burn["stress"]["id"], "gpu_burn")
        self.assertEqual(nv_burn["vram"]["id"], "gpu_vram_test")

        # Intel / AMD (pas de NVIDIA) -> OpenCL oclburn puissance + VRAM
        ocl = cp.select_gpu_tools(
            tool_check=lambda t: t in ("gcc", "clpeak"),
            has_nvidia=False,
        )
        self.assertEqual(ocl["stress"]["id"], "gpu_oclburn")
        self.assertEqual(ocl["stress"]["params"]["mode"], "power")
        self.assertEqual(ocl["vram"]["id"], "gpu_oclburn")
        self.assertEqual(ocl["vram"]["params"]["mode"], "memory")
        self.assertEqual(ocl["perf"]["id"], "gpu_vram_bw")

        # Repli Vulkan seul
        vk = cp.select_gpu_tools(
            tool_check=lambda t: t == "vkpeak",
            has_nvidia=False,
        )
        self.assertEqual(vk["stress"]["id"], "gpu_vkpeak")
        self.assertIsNone(vk["vram"])

    def test_all_profiles_tiers_modes_and_perf_option_validate(self):
        gpu_nv = cp.select_gpu_tools(
            tool_check=lambda t: t in ("gpu-fryer", "gpu-burn", "nvbandwidth"),
            has_nvidia=True,
        )
        perf_ids = {"ram_stream", "gpu_nvbandwidth", "gpu_vram_bw", "peak_suite_all"}

        for prof in cp.SEQUENCE_PROFILES:
            pid = prof["id"]
            for tier in cp.DURATION_TIERS:
                tid = tier["id"]
                for mode in ("par", "seq"):
                    for include_perf in (False, True):
                        spec = cp.build_profile_blocks(
                            pid, tier=tid, mode=mode, include_perf=include_perf, gpu_tools=gpu_nv
                        )
                        steps = cp.blocks_to_steps(
                            spec["blocks"], fio_path="/dev/nvme0n1,/dev/nvme1n1", target_gpus="0,1"
                        )
                        errs = validate_steps(steps)
                        self.assertEqual(
                            errs, [], msg=f"{pid}/{tid}/{mode}/perf={include_perf} invalide : {errs}"
                        )
                        flat = [s for st in steps for s in (st.get("parallel") or [st])]
                        ids_used = {s.get("bench") or s.get("stress") or s.get("fio") for s in flat}

                        # Vérifier absence / présence des composants selon le profil
                        if pid == "cpu_ram_gpu":
                            self.assertFalse(any("fio" in s for s in flat))
                            self.assertTrue(any(str(s.get("bench", "")).startswith("gpu_") for s in flat))
                        elif pid == "cpu_ram_ssd":
                            self.assertFalse(any(str(s.get("bench", "")).startswith("gpu_") for s in flat))
                            self.assertTrue(any("fio" in s for s in flat))
                        elif pid == "cpu_ram_gpu_ssd":
                            self.assertTrue(any("fio" in s for s in flat))
                            self.assertTrue(any(str(s.get("bench", "")).startswith("gpu_") for s in flat))

                        # Performance uniquement si demandée, et placée dans le dernier bloc
                        if not include_perf:
                            self.assertEqual(ids_used & perf_ids, set())
                        else:
                            last_ids = {i.get("id") for i in spec["blocks"][-1]["items"]}
                            self.assertTrue(bool(last_ids & perf_ids))
                            earlier_ids = {i.get("id") for b in spec["blocks"][:-1] for i in b["items"]}
                            self.assertEqual(earlier_ids & perf_ids, set())

                        # Mode parallèle : 1er bloc en parallèle avec 2+ tests
                        if mode == "par":
                            self.assertIn("parallel", steps[0])
                            self.assertGreaterEqual(len(steps[0]["parallel"]), 2)
                        else:
                            self.assertNotIn("parallel", steps[0])

    def test_multidisk_paths_and_conflict_detection(self):
        step_conflict = {
            "parallel": [
                {"fio": "heat_rand_mixed_1h", "path": "/dev/nvme0n1,/dev/nvme1n1"},
                {"fio": "stab_verify_crc_30m", "path": "/dev/nvme1n1,/dev/nvme2n1"},
            ]
        }
        warns = parallel_conflicts(step_conflict)
        self.assertTrue(any("même disque" in w for w in warns))

        step_ok = {
            "parallel": [
                {"fio": "heat_rand_mixed_1h", "path": "/dev/nvme0n1,/dev/nvme1n1"},
                {"fio": "stab_verify_crc_30m", "path": "/dev/nvme2n1"},
            ]
        }
        self.assertEqual(parallel_conflicts(step_ok), [])

    def test_campaign_folder_includes_profile_slug(self):
        d = cp.make_run_dir("/tmp/out", {"system_name": "srv01"}, "cpu_ram_gpu_ssd")
        self.assertIn("cpu_ram_gpu_ssd", d)


if __name__ == "__main__":
    unittest.main()
