from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from selenium.common.exceptions import NoSuchElementException


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


class NoToastDriver:
    @staticmethod
    def find_element(*_args):
        raise NoSuchElementException()


def patched_source(patch) -> str:
    return (
        f"{patch.REPLY_CONFIRMATION_BEFORE}\n"
        f"def patched():\n{patch.REPLY_ICON_CLICK_BEFORE}    return None\n"
    )


def test_patch_adds_reply_click_and_confirmation_fallbacks(tmp_path: Path) -> None:
    patch = load_patch_module()
    target = write_reply_handler(tmp_path, patched_source(patch))

    assert patch.patch_x_use(tmp_path) is True

    content = target.read_text(encoding="utf-8")
    assert patch.REPLY_ICON_CLICK_BEFORE not in content
    assert patch.REPLY_ICON_CLICK_AFTER in content
    assert patch.REPLY_CONFIRMATION_BEFORE not in content
    assert patch.REPLY_CONFIRMATION_AFTER in content
    assert "except ElementClickInterceptedException" in content
    assert 'driver.execute_script("arguments[0].click();", reply_icon_button)' in content
    assert "composer_cleared" in content
    assert 'textarea.get_attribute("textContent")' in content
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
    assert content.count(patch.REPLY_CONFIRMATION_BEFORE) == 1
    assert patch.REPLY_CONFIRMATION_AFTER not in content
    target = write_reply_handler(tmp_path, content)
    assert patch.patch_x_use(tmp_path) is True
    compile(target.read_text(encoding="utf-8"), str(target), "exec")


def test_confirmation_accepts_a_cleared_still_mounted_composer(tmp_path: Path) -> None:
    patch = load_patch_module()
    spec = importlib.util.find_spec("xuse")
    assert spec is not None and spec.submodule_search_locations
    package_root = Path(next(iter(spec.submodule_search_locations)))
    installed = package_root / patch.REPLY_HANDLER.relative_to("src/xuse")
    target = write_reply_handler(tmp_path, installed.read_text(encoding="utf-8"))
    assert patch.patch_x_use(tmp_path) is True

    module_spec = importlib.util.spec_from_file_location(
        "patched_reply_handler_for_test", target
    )
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)

    class ClearedTextarea:
        text = ""

        @staticmethod
        def is_enabled() -> bool:
            return True

        @staticmethod
        def get_attribute(name: str) -> str:
            assert name == "textContent"
            return ""

    class AttachedDialog:
        @staticmethod
        def is_enabled() -> bool:
            return True

    assert module._confirm_reply_submission(
        NoToastDriver(), AttachedDialog(), ClearedTextarea(), timeout=0.01
    ) is True


def test_confirmation_rejects_an_unchanged_composer_without_other_signal(
    tmp_path: Path,
) -> None:
    patch = load_patch_module()
    spec = importlib.util.find_spec("xuse")
    assert spec is not None and spec.submodule_search_locations
    package_root = Path(next(iter(spec.submodule_search_locations)))
    installed = package_root / patch.REPLY_HANDLER.relative_to("src/xuse")
    target = write_reply_handler(tmp_path, installed.read_text(encoding="utf-8"))
    assert patch.patch_x_use(tmp_path) is True

    module_spec = importlib.util.spec_from_file_location(
        "patched_reply_handler_still_filled_for_test", target
    )
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)

    class FilledTextarea:
        text = "expected reply"

        @staticmethod
        def is_enabled() -> bool:
            return True

        @staticmethod
        def get_attribute(name: str) -> str:
            assert name == "textContent"
            return "expected reply"

    class AttachedDialog:
        @staticmethod
        def is_enabled() -> bool:
            return True

    assert module._confirm_reply_submission(
        NoToastDriver(), AttachedDialog(), FilledTextarea(), timeout=0.01
    ) is False
