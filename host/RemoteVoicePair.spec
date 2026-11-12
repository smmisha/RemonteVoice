# One dependency graph and shared COLLECT prevent divergent base_library.zip files.
# https://pyinstaller.org/en/stable/spec-files.html#multipackaging-with-one-folder-apps
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

host = Path(SPECPATH).resolve()
datas, binaries, hiddenimports = [], [], ["websockets.asyncio.server"]
for package in ("qrcode", "PIL", "soundcard"):
    package_datas, package_binaries, package_imports = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_imports

# The Tk window sets its own icon from the bundled copy; PyInstaller datas land
# in _internal, which is also sys._MEIPASS at runtime.
datas.append((str(host / "RemoteVoice.ico"), "."))

a = Analysis(
    [str(host / "broker_launcher.py"), str(host / "launcher.py")],
    pathex=[str(host)], binaries=binaries, datas=datas,
    hiddenimports=hiddenimports, hookspath=[], hooksconfig={},
    runtime_hooks=[], excludes=[], noarchive=False, optimize=0,
)
pyz = PYZ(a.pure)


def entry_scripts(entry):
    entries = {"broker_launcher", "launcher"}
    for name in entries:
        matching = [script for script in a.scripts if script[0] == name]
        if (len(matching) != 1 or Path(matching[0][1]).resolve() != host / (name + ".py")
                or matching[0][2] != "PYSOURCE"):
            raise ValueError("Expected exactly one verified application entry script")
    if any(script[0] not in entries and not script[0].startswith("pyi_rth_") for script in a.scripts):
        raise ValueError("Unexpected executable script in shared analysis")
    return [script for script in a.scripts if script[0] not in entries or script[0] == entry]


broker = EXE(
    pyz, entry_scripts("broker_launcher"), [], exclude_binaries=True,
    name="RemoteVoiceBroker", icon=str(host / "RemoteVoice.ico"), console=True, debug=False, strip=False, upx=False,
    contents_directory="_internal", bootloader_ignore_signals=False,
)
client = EXE(
    pyz, entry_scripts("launcher"), [], exclude_binaries=True,
    name="RemoteVoice", icon=str(host / "RemoteVoice.ico"), console=False, debug=False, strip=False, upx=False,
    contents_directory="_internal", bootloader_ignore_signals=False,
)
pair = COLLECT(
    broker, client, a.binaries, a.datas,
    strip=False, upx=False, name="RemoteVoicePair",
)

