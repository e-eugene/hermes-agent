from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
PATCH_PATH = ROOT / "runtime" / "patch_x_use.py"


def load_patch_module():
    spec = importlib.util.spec_from_file_location("patch_x_use", PATCH_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_reply_handler(tmp_path: Path, content: str) -> Path:
    target = (
        tmp_path / "src" / "xuse" / "features" / "publisher" / "reply_handler.py"
    )
    target.parent.mkdir(parents=True)
    target.write_text(content, encoding="utf-8")
    return target


def test_patch_adds_intercepted_reply_icon_fallback(tmp_path: Path) -> None:
    patch = load_patch_module()
    target = write_reply_handler(
        tmp_path,
        f"def patched():\n{patch.REPLY_ICON_CLICK_BEFORE}    return None\n",
    )

    assert patch.patch_x_use(tmp_path) is True

    content = target.read_text(encoding="utf-8")
    assert patch.REPLY_ICON_CLICK_BEFORE not in content
    assert patch.REPLY_ICON_CLICK_AFTER in content
    assert "except ElementClickInterceptedException" in content
    assert 'driver.execute_script("arguments[0].click();", reply_icon_button)' in content
    assert patch.patch_x_use(tmp_path) is False


def test_patch_fails_closed_when_pinned_source_changes(tmp_path: Path) -> None:
    patch = load_patch_module()
    write_reply_handler(tmp_path, "# unexpected upstream source\n")

    with pytest.raises(RuntimeError, match="no longer matches"):
        patch.patch_x_use(tmp_path)


def test_patch_matches_the_installed_pinned_x_use_source(tmp_path: Path) -> None:
    patch = load_patch_module()
    spec = importlib.util.find_spec("xuse")
    assert spec is not None and spec.submodule_search_locations
    package_root = Path(next(iter(spec.submodule_search_locations)))
    installed = package_root / patch.REPLY_HANDLER.relative_to("src/xuse")
    content = installed.read_text(encoding="utf-8")

    assert content.count(patch.REPLY_ICON_CLICK_BEFORE) == 1
    assert patch.REPLY_ICON_CLICK_AFTER not in content
    target = write_reply_handler(tmp_path, content)
    assert patch.patch_x_use(tmp_path) is True
    compile(target.read_text(encoding="utf-8"), str(target), "exec")
