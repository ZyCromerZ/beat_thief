"""Persistent user settings, stored in config.json at the project root."""
import ctypes
import json
import os
import sys
from pathlib import Path
from uuid import UUID

# BEATTHIEF_CONFIG lets tests (or a second instance) use a different settings file.
CONFIG_FILE = Path(os.environ.get("BEATTHIEF_CONFIG") or Path(__file__).resolve().parent.parent / "config.json")

def _get_default_music_dir() -> Path:
    """Return the OS-configured Music directory, falling back to ~/Music."""
    if sys.platform == "win32":
        try:
            # FOLDERID_Music: {4BD8D571-6D19-48D3-BE97-422220080E43}
            folder_id = UUID("{4BD8D571-6D19-48D3-BE97-422220080E43}").bytes_le
            path_ptr = ctypes.c_wchar_p()

            sh_get_known_folder_path = ctypes.windll.shell32.SHGetKnownFolderPath
            sh_get_known_folder_path.argtypes = [
                ctypes.c_char_p,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_wchar_p),
            ]
            sh_get_known_folder_path.restype = ctypes.c_long

            co_task_mem_free = ctypes.windll.ole32.CoTaskMemFree
            co_task_mem_free.argtypes = [ctypes.c_void_p]

            hr = sh_get_known_folder_path(
                ctypes.create_string_buffer(folder_id),
                0,
                None,
                ctypes.byref(path_ptr),
            )
            if hr == 0 and path_ptr.value:
                music_path = Path(path_ptr.value)
                co_task_mem_free(path_ptr)
                return music_path
        except Exception:
            pass
    return Path.home() / "Music"

DEFAULT_DOWNLOAD_DIR = _get_default_music_dir() / "Unclassified"

def _load() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def get_setting(key: str, default=None):
    return _load().get(key, default)


def set_setting(key: str, value):
    """Persist one setting; a value of None removes it (back to the default)."""
    data = _load()
    if value is None:
        data.pop(key, None)
    else:
        data[key] = value
    CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def get_download_dir() -> Path:
    """Current download directory (read on every call so changes apply immediately)."""
    value = _load().get("download_dir")
    return Path(value).expanduser() if value else DEFAULT_DOWNLOAD_DIR


def set_download_dir(path: str) -> Path:
    """Validate, create and persist a new download directory. Raises ValueError."""
    path = (path or "").strip()
    if not path:
        raise ValueError("The path cannot be empty.")

    target = Path(path).expanduser()
    if not target.is_absolute():
        raise ValueError("Use an absolute path (or one starting with ~).")
    target = target.resolve()

    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ValueError(f"Cannot create the directory: {e.strerror or e}") from e
    if not target.is_dir() or not os.access(target, os.W_OK | os.X_OK):
        raise ValueError("The directory is not writable.")

    set_setting("download_dir", str(target))
    return target
