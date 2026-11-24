"""端到端测试：备份库多版本、按内容去重、独立恢复、删除回收与崩溃一致性。

覆盖：
- 首次保存创建库、重名拒绝、forget 后同名可重存；
- 只保存 manifest/structure/清单页，不碰旁边的文件；
- 库内字节相同载荷只留一份（跨版本、跨相对路径），同路径不同内容不混淆；
- 恢复得到独立可查询的档案，原相对路径与原始字节一致；
- 校验不通过的档案不能成为版本；库内对象缺失/损坏时恢复失败且不留半个档案；
- forget 不损坏其他版本，无引用载荷被回收并可被后续保存复用；
- 在途恢复的租约保护对象，恢复结束/进程退出后下一次成功保存或删除时清理；
- 保存/删除被中断后重新打开库，版本呈现操作前或操作后的完整状态；
- 恢复期间其他进程可删除并重写同名版本，恢复仍得到开始时选定的版本。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from qcarchive import (  # noqa: E402
    ArchiveBuilder,
    Vault,
    VaultError,
    query_archive,
)
from qcarchive.errors import ArchiveError  # noqa: E402
from qcarchive.vault import (  # noqa: E402
    LEASES_DIR,
    OBJECTS_DIR,
    OWNER_FILE,
    TEMP_DIR,
    VERSIONS_DIR,
)

RUN = [sys.executable, "-I", str(REPO / "scripts" / "run_query.py")]


def build_archive(root: Path, *, detail0: str = "明细0", remark: str = "备注A",
                  extra: dict | None = None, page_sizes: dict | None = None) -> Path:
    b = ArchiveBuilder()
    e0 = b.add_equipment()
    c0 = b.add_component(e0, remark=remark)
    b.add_inspection(c0, "耐压试验", "合格", detail0)
    b.add_inspection(c0, "外观检查", "不合格", "明细1")
    b.add_inspection(c0, "耐压试验", "不合格", "明细2")
    c1 = b.add_component(e0, remark=None)
    b.add_inspection(c1, "耐压试验", "不合格", "明细4")
    b.add_equipment()
    arc = b.build(root, page_sizes=page_sizes or {
        "inspection_category": 2, "inspection_result": 3,
        "inspection_detail": 2, "component_remark": 1,
    })
    if extra:
        for name, data in extra.items():
            (arc / name).write_bytes(data)
    return arc


def tree_bytes(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): p.read_bytes().hex()
            for p in sorted(root.rglob("*")) if p.is_file()}


def object_files(vault: Path) -> list[Path]:
    root = vault / OBJECTS_DIR
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*") if p.is_file())


def version_files(vault: Path) -> list[Path]:
    root = vault / VERSIONS_DIR
    return sorted(p for p in root.glob("v-*.json")) if root.is_dir() else []


def run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(RUN + list(args), capture_output=True, text=True)


class SaveRestoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_first_save_creates_vault_and_roundtrips(self) -> None:
        src = build_archive(self.tmp / "src")
        vault = self.tmp / "vault"
        Vault(vault).save("2025-01", src)
        self.assertTrue((vault / "vault.json").is_file())
        out = self.tmp / "restored"
        Vault(vault).restore("2025-01", out)
        self.assertEqual(tree_bytes(src), tree_bytes(out))
        # 恢复出的档案可以脱离备份库独立查询
        report = query_archive(out, "耐压试验", "不合格")
        self.assertEqual([h.inspection_slot for h in report.hits], [2, 3])

    def test_only_archive_files_saved(self) -> None:
        src = build_archive(self.tmp / "src", extra={
            "notes.txt": b"do not save", "pages/extra.json": b"{}",
        })
        vault = self.tmp / "vault"
        Vault(vault).save("v", src)
        out = self.tmp / "out"
        Vault(vault).restore("v", out)
        self.assertNotIn("notes.txt", tree_bytes(out))
        self.assertNotIn("pages/extra.json", tree_bytes(out))
        self.assertIn("manifest.json", tree_bytes(out))
        self.assertIn("structure.json", tree_bytes(out))

    def test_duplicate_name_rejected_and_reusable_after_forget(self) -> None:
        src1 = build_archive(self.tmp / "a1", detail0="first")
        src2 = build_archive(self.tmp / "a2", detail0="second")
        vault = self.tmp / "vault"
        Vault(vault).save("m", src1)
        with self.assertRaises(VaultError):
            Vault(vault).save("m", src2)
        # 原版本未被替换
        out1 = self.tmp / "r1"
        Vault(vault).restore("m", out1)
        self.assertEqual(tree_bytes(src1), tree_bytes(out1))

        Vault(vault).forget("m")
        Vault(vault).save("m", src2)
        out2 = self.tmp / "r2"
        Vault(vault).restore("m", out2)
        self.assertEqual(tree_bytes(src2), tree_bytes(out2))

    def test_invalid_archive_never_becomes_version(self) -> None:
        vault = self.tmp / "vault"
        Vault(vault).save("good", build_archive(self.tmp / "good"))

        corrupt = build_archive(self.tmp / "corrupt")
        page = corrupt / "pages" / "inspection_detail" / "p0.json"
        page.write_bytes(page.read_bytes() + b"x")
        count_before = len(version_files(vault))
        with self.assertRaises(VaultError):
            Vault(vault).save("bad", corrupt)
        self.assertEqual(len(version_files(vault)), count_before)
        with self.assertRaises(VaultError):
            Vault(vault).restore("bad", self.tmp / "nope")

        # 缺失 manifest 同样不能保存
        broken = build_archive(self.tmp / "broken")
        (broken / "manifest.json").unlink()
        with self.assertRaises(VaultError):
            Vault(vault).save("bad2", broken)

    def test_failed_save_does_not_create_vault(self) -> None:
        corrupt = build_archive(self.tmp / "corrupt")
        (corrupt / "manifest.json").unlink()
        vault = self.tmp / "newvault"
        with self.assertRaises(VaultError):
            Vault(vault).save("x", corrupt)
        self.assertFalse(vault.exists())


class DedupAndForgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_identical_payload_single_copy_across_versions(self) -> None:
        src1 = build_archive(self.tmp / "a1", detail0="one", remark="R1")
        src2 = build_archive(self.tmp / "a2", detail0="two", remark="R2")
        vault = self.tmp / "vault"
        Vault(vault).save("v1", src1)
        objs1 = {p.name for p in object_files(vault)}
        Vault(vault).save("v2", src2)
        objs2 = {p.name for p in object_files(vault)}

        # v1 的对象仍全部保留，v2 只新增真正不同的内容
        self.assertTrue(objs1 <= objs2)
        self.assertLess(len(objs2 - objs1), len(objs2))
        # 每个对象路径就是其内容摘要，且全库无重复内容
        names = [p.name for p in object_files(vault)]
        self.assertEqual(len(names), len(set(names)))
        digests = {hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in object_files(vault)}
        self.assertEqual(digests, set(names))

    def test_identical_bytes_deduped_across_relative_paths(self) -> None:
        # 同一字节内容出现在不同相对路径时，库内只保留一份对象。
        # 用两份结构相同、内容相同的档案：它们的每一个页字节都相同，
        # 第二份保存不应新增对象，对象名始终等于内容摘要。
        arc1 = build_archive(self.tmp / "a1")
        arc2 = build_archive(self.tmp / "a2")
        vault = self.tmp / "vault"
        Vault(vault).save("one", arc1)
        n1 = len(object_files(vault))
        Vault(vault).save("two", arc2)
        objects = object_files(vault)
        self.assertEqual(len(objects), n1)  # 内容相同不重复存储
        for p in objects:
            self.assertEqual(p.name, hashlib.sha256(p.read_bytes()).hexdigest())
        # 跨相对路径也不重复：对象集合就是全库不同字节内容的集合。
        expected = {hashlib.sha256(bytes.fromhex(hexed)).hexdigest()
                    for hexed in tree_bytes(arc1).values()}
        self.assertEqual({p.name for p in objects}, expected)

    def test_same_path_different_content_not_substituted(self) -> None:
        src1 = build_archive(self.tmp / "a1", detail0="version-one")
        src2 = build_archive(self.tmp / "a2", detail0="version-two")
        vault = self.tmp / "vault"
        Vault(vault).save("one", src1)
        Vault(vault).save("two", src2)
        for name, src in (("one", src1), ("two", src2)):
            out = self.tmp / f"out-{name}"
            Vault(vault).restore(name, out)
            self.assertEqual(tree_bytes(src), tree_bytes(out))
        self.assertEqual(len(object_files(vault)) > 0, True)

    def test_forget_keeps_other_versions_and_reclaims_space(self) -> None:
        common = build_archive(self.tmp / "common")
        other = build_archive(self.tmp / "other", detail0="unique-detail")
        vault = self.tmp / "vault"
        Vault(vault).save("keep", common)
        Vault(vault).save("drop", other)
        before = len(object_files(vault))

        Vault(vault).forget("drop")
        after = len(object_files(vault))
        self.assertLess(after, before)

        # 其他版本完好可恢复
        out = self.tmp / "out"
        Vault(vault).restore("keep", out)
        self.assertEqual(tree_bytes(common), tree_bytes(out))
        with self.assertRaises(VaultError):
            Vault(vault).restore("drop", self.tmp / "gone")

        # 回收的空间可被后续保存复用：重存 drop 不改变对象数
        Vault(vault).save("drop2", other)
        self.assertEqual(len(object_files(vault)), before)

    def test_forget_missing_version_fails(self) -> None:
        vault = self.tmp / "vault"
        Vault(vault).save("a", build_archive(self.tmp / "a"))
        with self.assertRaises(VaultError):
            Vault(vault).forget("b")
        self.assertEqual(len(version_files(vault)), 1)


class CorruptVaultTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_missing_object_restore_fails_without_half_archive(self) -> None:
        src = build_archive(self.tmp / "src")
        vault = self.tmp / "vault"
        Vault(vault).save("v", src)
        # 破坏对象：删除一个页对象
        objects = object_files(vault)
        page_obj = None
        for p in objects:
            if p.name == hashlib.sha256(
                    (src / "pages/inspection_detail/p0.json").read_bytes()).hexdigest():
                page_obj = p
                break
        self.assertIsNotNone(page_obj)
        page_obj.unlink()
        out = self.tmp / "out"
        with self.assertRaises(ArchiveError):
            Vault(vault).restore("v", out)
        self.assertFalse(out.exists())
        leftover = [p.name for p in self.tmp.iterdir() if ".restore-" in p.name]
        self.assertEqual(leftover, [])

    def test_corrupt_object_restore_fails_without_half_archive(self) -> None:
        src = build_archive(self.tmp / "src")
        vault = self.tmp / "vault"
        Vault(vault).save("v", src)
        # 损坏一个对象
        obj = object_files(vault)[0]
        obj.write_bytes(b"tampered")
        out = self.tmp / "out"
        with self.assertRaises(ArchiveError):
            Vault(vault).restore("v", out)
        self.assertFalse(out.exists())
        leftover = [p.name for p in self.tmp.iterdir() if ".restore-" in p.name]
        self.assertEqual(leftover, [])

    def test_restore_refuses_existing_target(self) -> None:
        src = build_archive(self.tmp / "src")
        vault = self.tmp / "vault"
        Vault(vault).save("v", src)
        target = self.tmp / "exists"
        target.mkdir()
        with self.assertRaises(VaultError):
            Vault(vault).restore("v", target)
        self.assertEqual(list(target.iterdir()), [])


class CrashAndConcurrencyTests(unittest.TestCase):
    """保存/删除中断、恢复期间删除重存 —— 通过子进程模拟。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run_interrupted_save(self, vault: Path, src: Path, name: str,
                              kill_after_objects: int) -> None:
        """在保存子进程发布若干对象后将其 SIGKILL。"""
        script = textwrap.dedent(f"""
            import os, sys
            sys.path.insert(0, {str(REPO / "src")!r})
            import qcarchive.vault as v
            original = v.Vault._publish_staged
            count = [0]
            def patched(self, staging, digest):
                if count[0] >= {kill_after_objects}:
                    os.kill(os.getpid(), 9)
                count[0] += 1
                return original(self, staging, digest)
            v.Vault._publish_staged = patched
            v.Vault({str(vault)!r}).save({name!r}, {str(src)!r})
        """)
        proc = subprocess.run([sys.executable, "-I", "-c", script],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, -9, proc.stderr)

    def test_interrupted_save_leaves_pre_state_and_is_reusable(self) -> None:
        src1 = build_archive(self.tmp / "a1", detail0="one")
        src2 = build_archive(self.tmp / "a2", detail0="two")
        vault = self.tmp / "vault"
        Vault(vault).save("existing", src1)
        before_versions = [p.name for p in version_files(vault)]

        self._run_interrupted_save(vault, src2, "half", kill_after_objects=3)
        # 中断后版本状态与操作前一致：没有半套版本
        self.assertEqual([p.name for p in version_files(vault)], before_versions)
        with self.assertRaises(VaultError):
            Vault(vault).restore("half", self.tmp / "x")
        # 可直接继续操作，且下次成功保存回收中断残留
        Vault(vault).save("half", src2)
        out = self.tmp / "out"
        Vault(vault).restore("half", out)
        self.assertEqual(tree_bytes(src2), tree_bytes(out))
        self.assertEqual([p.name for p in (vault / TEMP_DIR).iterdir()], [])

    def test_interrupted_forget_keeps_version_or_removes_it_cleanly(self) -> None:
        # 删除只有“记录原子消失”一种中间态；模拟在删记录后、回收前被杀。
        src = build_archive(self.tmp / "src")
        vault = self.tmp / "vault"
        Vault(vault).save("v", src)
        script = textwrap.dedent(f"""
            import os, sys
            sys.path.insert(0, {str(REPO / "src")!r})
            import qcarchive.vault as v
            def boom(self):
                os.kill(os.getpid(), 9)
            v.Vault._collect_garbage = boom
            v.Vault({str(vault)!r}).forget('v')
        """)
        proc = subprocess.run([sys.executable, "-I", "-c", script],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, -9, proc.stderr)
        # 重新打开库：版本已完整删除，可继续操作
        out = self.tmp / "out"
        with self.assertRaises(VaultError):
            Vault(vault).restore("v", out)
        Vault(vault).save("v2", src)
        Vault(vault).restore("v2", out)
        self.assertEqual(tree_bytes(src), tree_bytes(out))

    def test_restore_snapshot_survives_forget_and_name_reuse(self) -> None:
        """恢复锁定版本后，删除并同名重存不影响本次恢复。"""
        old = build_archive(self.tmp / "old", detail0="old-detail")
        new = build_archive(self.tmp / "new", detail0="new-detail")
        vault = self.tmp / "vault"
        Vault(vault).save("m", old)

        hold = self.tmp / "hold"
        hold.write_text("go", encoding="utf-8")
        release = self.tmp / "release"
        script = textwrap.dedent(f"""
            import sys, time, pathlib
            sys.path.insert(0, {str(REPO / "src")!r})
            import qcarchive.vault as v
            real = v._copy_object_verified
            count = [0]
            def slow(src, dst, expected):
                result = real(src, dst, expected)
                count[0] += 1
                if count[0] == 1:
                    pathlib.Path({str(hold)!r}).write_text('started')
                    while not pathlib.Path({str(release)!r}).exists():
                        time.sleep(0.02)
                return result
            v._copy_object_verified = slow
            v.Vault({str(vault)!r}).restore('m', {str(self.tmp / 'restored')!r})
        """)
        proc = subprocess.Popen([sys.executable, "-I", "-c", script])
        try:
            deadline = time.time() + 20
            while time.time() < deadline:
                if hold.exists() and hold.read_text() == "started":
                    break
                time.sleep(0.02)
            else:
                self.fail("restore did not start copying")

            # 恢复已开始复制：其他进程可删除并同名重存，且不必等它复制完。
            t0 = time.time()
            proc_forget = run_cli("vault", "forget", str(vault), "m")
            self.assertEqual(proc_forget.returncode, 0, proc_forget.stderr)
            proc_save = run_cli("vault", "save", str(vault), "m", str(new))
            self.assertEqual(proc_save.returncode, 0, proc_save.stderr)
            self.assertLess(time.time() - t0, 15)
            # 恢复期间租约保护了旧对象：旧版本载荷仍存在
            release.write_text("go", encoding="utf-8")
            self.assertEqual(proc.wait(timeout=30), 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        # 本次恢复得到的是开始复制时选定的旧版本，而非同名新版本
        self.assertEqual(tree_bytes(old), tree_bytes(self.tmp / "restored"))
        # 恢复结束后租约清理，旧版无引用载荷被回收；新版本仍完好
        out_new = self.tmp / "new-restored"
        Vault(vault).restore("m", out_new)
        self.assertEqual(tree_bytes(new), tree_bytes(out_new))
        lease_dirs = [p for p in (vault / LEASES_DIR).iterdir()] \
            if (vault / LEASES_DIR).is_dir() else []
        self.assertEqual(lease_dirs, [])

    def test_parallel_saves_and_restores(self) -> None:
        src = build_archive(self.tmp / "src")
        vault = self.tmp / "vault"
        procs = [
            subprocess.Popen(
                RUN + ["vault", "save", str(vault), f"n{i}", str(src)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for i in range(4)
        ]
        for i, proc in enumerate(procs):
            _out, err = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 0, f"save n{i}: {err}")

        outs = self.tmp / "outs"
        outs.mkdir()
        procs = [
            subprocess.Popen(
                RUN + ["vault", "restore", str(vault), f"n{i}",
                       "--out", str(outs / str(i))],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for i in range(4)
        ]
        for i, proc in enumerate(procs):
            _out, err = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 0, f"restore n{i}: {err}")
        expected = tree_bytes(src)
        for i in range(4):
            self.assertEqual(tree_bytes(outs / str(i)), expected)
        # 内容完全相同的并发版本只存一份对象
        digests = {hashlib.sha256(bytes.fromhex(h)).hexdigest()
                   for h in expected.values()}
        self.assertEqual({p.name for p in object_files(vault)}, digests)
        self.assertEqual(list((vault / LEASES_DIR).iterdir()), [])

    def test_dead_restore_lease_collected_on_next_success(self) -> None:
        """恢复进程被杀死后，下一次成功保存/删除清理其租约与无引用对象。"""
        src = build_archive(self.tmp / "src")
        vault = self.tmp / "vault"
        Vault(vault).save("m", src)
        # 手工制造一个“已死进程”的租约（PID 不存在）
        lease = vault / LEASES_DIR / "lease-deadbeef"
        lease.mkdir()
        (lease / OWNER_FILE).write_text(
            json.dumps({"pid": 999999, "start": "1"}), encoding="utf-8")
        (lease / "objects.json").write_text(
            json.dumps({"version": "m",
                        "objects": [p.name for p in object_files(vault)]}),
            encoding="utf-8")

        # 正常版本仍在，恢复不受影响
        out = self.tmp / "out"
        Vault(vault).restore("m", out)
        self.assertEqual(tree_bytes(src), tree_bytes(out))
        self.assertFalse(lease.exists())

        # 删除版本 + 死租约残留 -> 下一次成功删除后对象全部回收
        Vault(vault).save("unused", build_archive(self.tmp / "unused",
                                                  detail0="temp"))
        Vault(vault).forget("unused")
        Vault(vault).forget("m")
        self.assertEqual(object_files(vault), [])


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_cli_save_restore_forget(self) -> None:
        src = build_archive(self.tmp / "src")
        vault = self.tmp / "vault"
        r = run_cli("vault", "save", str(vault), "jan", str(src))
        self.assertEqual(r.returncode, 0, r.stderr)

        r = run_cli("vault", "save", str(vault), "jan", str(src))
        self.assertEqual(r.returncode, 2)

        out = self.tmp / "out"
        r = run_cli("vault", "restore", str(vault), "jan", "--out", str(out))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(tree_bytes(src), tree_bytes(out))

        r = run_cli("vault", "forget", str(vault), "jan")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = run_cli("vault", "restore", str(vault), "jan", "--out", str(out))
        self.assertEqual(r.returncode, 2)

    def test_cli_invalid_name_and_bad_vault(self) -> None:
        src = build_archive(self.tmp / "src")
        vault = self.tmp / "vault"
        self.assertEqual(
            run_cli("vault", "save", str(vault), "", str(src)).returncode, 3)
        notvault = self.tmp / "notvault"
        notvault.mkdir()
        (notvault / "x").write_text("x", encoding="utf-8")
        self.assertEqual(
            run_cli("vault", "save", str(notvault), "a", str(src)).returncode, 2)

    def test_cli_query_unchanged_on_restored_archive(self) -> None:
        src = build_archive(self.tmp / "src")
        vault = self.tmp / "vault"
        self.assertEqual(
            run_cli("vault", "save", str(vault), "jan", str(src)).returncode, 0)
        out = self.tmp / "out"
        self.assertEqual(
            run_cli("vault", "restore", str(vault), "jan", "--out", str(out)).returncode,
            0)
        r = run_cli("query", str(out), "--category", "耐压试验", "--result", "不合格")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("命中 2 条", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
