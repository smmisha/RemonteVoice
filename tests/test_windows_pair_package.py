import ast
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from remontevoice.broker_package_manifest import MANIFEST_NAME, parse_manifest

ROOT = Path(__file__).resolve().parents[1]
HOST = ROOT / "host"
SPEC = HOST / "RemonteVoicePair.spec"
MODULE_SPEC = importlib.util.spec_from_file_location("build_windows_pair", HOST / "build_windows_pair.py")
builder = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(builder)


def evaluate_spec(extra_scripts=(), entry_path=None):
    calls, graph = [], types.SimpleNamespace()
    hooks = types.ModuleType("PyInstaller.utils.hooks")
    hooks.collect_all = lambda package: ([("data-" + package, ".")], [("bin-" + package, ".")], [package])
    def analysis(scripts, **options):
        calls.append(("analysis", scripts, options))
        graph.scripts = [("pyi_rth_inspect", "/hooks/pyi_rth_inspect.py", "PYSOURCE"),
                         ("broker_launcher", str(entry_path or HOST / "broker_launcher.py"), "PYSOURCE"),
                         ("launcher", str(HOST / "launcher.py"), "PYSOURCE"), *extra_scripts]
        graph.pure, graph.binaries, graph.datas = object(), options["binaries"], options["datas"]
        return graph
    def pyz(pure):
        result = object()
        calls.append(("pyz", pure, result))
        return result
    def executable(*parts, **options):
        result = object()
        calls.append(("exe", parts, options, result))
        return result
    def collect(*parts, **options):
        calls.append(("collect", parts, options))
    namespace = dict(SPECPATH=str(HOST), Analysis=analysis, PYZ=pyz, EXE=executable, COLLECT=collect)
    with patch.dict(sys.modules, {"PyInstaller.utils.hooks": hooks}):
        exec(compile(SPEC.read_text("utf-8"), str(SPEC), "exec"), namespace)
    return calls, graph


class PairSpecTests(unittest.TestCase):
    def test_one_graph_shared_archive_collection_and_exact_independent_entries(self):
        calls, graph = evaluate_spec()
        self.assertEqual(sum(call[0] == "analysis" for call in calls), 1)
        self.assertEqual(sum(call[0] == "pyz" for call in calls), 1)
        self.assertEqual(sum(call[0] == "collect" for call in calls), 1)
        executables = [call for call in calls if call[0] == "exe"]
        self.assertEqual(len(executables), 2)
        for call, entry, name, console in zip(executables, ("broker_launcher", "launcher"),
                                            ("RemonteVoiceBroker", "RemonteVoice"), (True, False)):
            self.assertEqual([script[0] for script in call[1][1]], ["pyi_rth_inspect", entry])
            self.assertEqual(call[2]["name"], name)
            self.assertIs(call[2]["console"], console)
            self.assertTrue(call[2]["exclude_binaries"])
            self.assertFalse(call[2]["upx"])
        self.assertIs(executables[0][1][0], executables[1][1][0])
        collection = calls[-1]
        self.assertEqual(collection[1][:2], tuple(call[3] for call in executables))
        self.assertIs(collection[1][2], graph.binaries)
        self.assertIs(collection[1][3], graph.datas)
        self.assertIn("websockets.asyncio.server", calls[0][2]["hiddenimports"])
        self.assertTrue({"qrcode", "PIL", "soundcard"} <= set(calls[0][2]["hiddenimports"]))
        tree = ast.parse(SPEC.read_text("utf-8"))
        names = [node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)]
        self.assertEqual(names.count("Analysis"), 1)
        self.assertEqual(names.count("COLLECT"), 1)

    def test_unexpected_duplicate_or_relocated_entry_scripts_fail_before_collection(self):
        for extra, path in (([("unknown", "/x.py", "PYSOURCE")], None),
                            ([("launcher", str(HOST / "launcher.py"), "PYSOURCE")], None),
                            ([], HOST / "other.py")):
            with self.subTest(extra=extra, path=path), self.assertRaises(ValueError):
                evaluate_spec(extra, path)


class PairBuilderTests(unittest.TestCase):
    def fake_bundler(self, command, **options):
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(command[1:3], ["-m", "PyInstaller"])
        self.assertNotIn("--clean", command)
        self.assertNotIn("--noconfirm", command)
        self.assertTrue(options["check"])
        self.assertEqual(options["stdin"], subprocess.DEVNULL)
        output = Path(command[command.index("--distpath") + 1]).parent
        for name in ("config", "cache", "work", "dist"):
            self.assertTrue((output / name).is_dir())
        self.assertEqual(options["env"]["PYINSTALLER_CONFIG_DIR"], str(output / "config"))
        package = output / "dist" / "RemonteVoicePair"
        for name, value in (("RemonteVoiceBroker.exe", b"broker"), ("RemonteVoice.exe", b"client"),
                            ("_internal/base_library.zip", b"base"), ("_internal/python314.dll", b"runtime")):
            path = package / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        options["stdout"].write(b"fake bundler successful\n")
        return subprocess.CompletedProcess(command, 0)

    def test_build_creates_new_package_manifest_and_external_evidence_without_real_pyinstaller(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            output = Path(temporary) / "new-build"
            with patch.object(builder.importlib.metadata, "version", return_value="existing-test-tool"):
                package = builder.build_pair(output, runner=self.fake_bundler)
            manifest = parse_manifest((package / MANIFEST_NAME).read_bytes())
            report = json.loads((output / "build-report.json").read_text("utf-8"))
            self.assertEqual(report["components"], {name: manifest.files[name] for name in
                                                   ("RemonteVoiceBroker.exe", "RemonteVoice.exe")})
            self.assertEqual(report["runtime_files"]["_internal/python314.dll"],
                             hashlib.sha256(b"runtime").hexdigest())
            self.assertIn("RemonteVoicePair.spec", report["source_sha256"])
            self.assertIn("remontevoice/broker_entry.py", report["source_sha256"])
            self.assertTrue((output / "pyinstaller.log").is_file())
            self.assertFalse((package / "build-report.json").exists())
            with self.assertRaises(FileExistsError):
                builder.build_pair(output, runner=self.fake_bundler)

    def test_failure_preserves_log_and_never_generates_acceptance_manifest(self):
        def failed(command, **options):
            options["stdout"].write(b"fake bundler failure\n")
            raise subprocess.CalledProcessError(1, command)
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            output = Path(temporary) / "failed"
            with self.assertRaises(subprocess.CalledProcessError):
                builder.build_pair(output, runner=failed)
            self.assertEqual((output / "pyinstaller.log").read_bytes(), b"fake bundler failure\n")
            self.assertFalse((output / "build-report.json").exists())

    def test_invalid_driver_and_source_changes_are_not_certified(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            output = Path(temporary) / "invalid"
            with self.assertRaises(ValueError):
                builder.build_pair(output, "unconfigured", runner=self.fake_bundler)
            self.assertFalse(output.exists())
            with patch.object(builder, "_sources", side_effect=[{"source": "a"}, {"source": "b"}]):
                with self.assertRaisesRegex(RuntimeError, "sources changed"):
                    builder.build_pair(output, runner=self.fake_bundler)
            self.assertFalse((output / "dist" / "RemonteVoicePair" / MANIFEST_NAME).exists())

    def test_cli_requires_output_and_does_not_run_without_it(self):
        with patch.object(builder, "build_pair") as build, patch("sys.stderr"):
            with self.assertRaises(SystemExit):
                builder.main([])
        build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
