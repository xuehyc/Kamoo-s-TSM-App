import os
import sys
import platform
from typing import Optional

MOCK_WARCRAFT_BASE = "fake_warcraft_base"


def get_temp_path() -> str:
    # windows %TEMP%, linux-like /tmp
    if os.name == "nt":
        return os.environ.get("TEMP")

    else:
        return "/tmp"


def ensure_path(path: str) -> None:
    # if not os.path.exists(path):
    #     os.makedirs(path)

    # make dirs recursively
    os.makedirs(path, exist_ok=True)


def remove_path(path: str, parent: bool = False) -> None:
    if os.path.exists(path):
        for file in os.listdir(path):
            os.remove(os.path.join(path, file))

        if parent:
            os.rmdir(path)


def remove_file(path: str):
    # exists as file
    if os.path.isfile(path):
        os.remove(path)


def find_warcraft_base() -> Optional[str]:
    if "unittest" in sys.modules:
        return MOCK_WARCRAFT_BASE

    if sys.platform == "win32":
        import winreg
    else:
        return None

    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\WOW6432Node\Blizzard Entertainment\World of Warcraft",
        )
        path = winreg.QueryValueEx(key, "InstallPath")[0]
        path = os.path.join(path, "..")
        return os.path.normpath(path)
    except Exception:
        return None


_GameVersionEnum = None


def validate_warcraft_base(path: str) -> bool:
    global _GameVersionEnum
    if not _GameVersionEnum:
        # unfortunate workaround to avoid circular import
        from ah.models import GameVersionEnum

        _GameVersionEnum = GameVersionEnum

    if not path or not os.path.isdir(path):
        return False

    # at least one version folder should exist
    version_dirs = (version.get_version_folder_name() for version in _GameVersionEnum)
    if not any(os.path.isdir(os.path.join(path, version)) for version in version_dirs):
        return False

    return True


def get_release_file_name(tag: str) -> str:
    return (
        "-".join(
            [
                platform.system().lower(),
                platform.machine().lower(),
                tag,
            ]
        )
        + ".zip"
    )
