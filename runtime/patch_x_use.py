#!/usr/bin/env python3
"""Apply audited Hermes fixes to the pinned x-use source tree."""

from __future__ import annotations

import sys
from pathlib import Path


REPLY_HANDLER = Path("src/xuse/features/publisher/reply_handler.py")

REPLY_CONFIRMATION_BEFORE = """\
def _confirm_reply_submission(driver, dialog, textarea, timeout: float = 10.0) -> bool:
    \"\"\"Wait for a post-submit confirmation signal.

    Modal path: the dialog detaches. Inline path (no dialog): a toast appears
    or the composer textarea detaches (re-render after a successful post).
    A reply that produced no signal is treated as not submitted.
    \"\"\"
    if dialog is not None:
        try:
            WebDriverWait(driver, timeout).until(EC.staleness_of(dialog))
            return True
        except Exception:
            return False
    conditions = [EC.presence_of_element_located((By.XPATH, REPLY_ANY_TOAST_XPATH))]
    if textarea is not None:
        conditions.append(EC.staleness_of(textarea))
    try:
        WebDriverWait(driver, timeout).until(EC.any_of(*conditions))
        return True
    except Exception:
        return False
"""

REPLY_CONFIRMATION_AFTER = """\
def _confirm_reply_submission(driver, dialog, textarea, timeout: float = 10.0) -> bool:
    \"\"\"Wait for a post-submit confirmation signal.

    X does not consistently detach its reply dialog or emit a toast after a
    successful post. In that variant the same contenteditable stays mounted
    but its text is cleared. Treat that cleared composer as confirmation too;
    the caller has already verified that the submit button was enabled and
    that no platform error toast appeared.
    \"\"\"

    def composer_cleared(_driver):
        if textarea is None:
            return False
        try:
            rendered_text = (textarea.text or \"\").strip()
            dom_text = (textarea.get_attribute(\"textContent\") or \"\").strip()
            return not rendered_text and not dom_text
        except Exception:
            return False

    conditions = [
        EC.presence_of_element_located((By.XPATH, REPLY_ANY_TOAST_XPATH))
    ]
    if dialog is not None:
        conditions.append(EC.staleness_of(dialog))
    if textarea is not None:
        conditions.extend((EC.staleness_of(textarea), composer_cleared))
    try:
        WebDriverWait(driver, timeout).until(EC.any_of(*conditions))
        return True
    except Exception:
        return False
"""

REPLY_ICON_CLICK_BEFORE = """\
        reply_icon_button = WebDriverWait(main_tweet_element, 10).until(
            EC.element_to_be_clickable((By.XPATH, ".//button[@data-testid='reply']"))
        )
        reply_icon_button.click()
        logger.info(f"Clicked reply icon for tweet {original_tweet.tweet_id}.")
"""

REPLY_ICON_CLICK_AFTER = """\
        reply_icon_button = WebDriverWait(main_tweet_element, 10).until(
            EC.element_to_be_clickable((By.XPATH, ".//button[@data-testid='reply']"))
        )
        # X can leave a transient composer/engagement overlay over the reply
        # icon even though Selenium considers the button clickable. Centering
        # the button avoids sticky viewport chrome; a DOM click is the same
        # bounded fallback already used for the final Reply submit button.
        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});",
                reply_icon_button,
            )
        except Exception:
            pass
        try:
            reply_icon_button.click()
        except ElementClickInterceptedException:
            logger.warning("Reply icon click intercepted, trying JS click.")
            try:
                driver.execute_script("arguments[0].click();", reply_icon_button)
            except Exception as click_error:
                logger.error(
                    "Failed to open reply composer after intercepted click: %s",
                    click_error,
                )
                return False
        logger.info(f"Clicked reply icon for tweet {original_tweet.tweet_id}.")
"""


def patch_x_use(source_root: Path) -> bool:
    """Patch the exact pinned source; fail closed on an unexpected revision."""

    target = source_root / REPLY_HANDLER
    content = target.read_text(encoding="utf-8")
    replacements = (
        (REPLY_CONFIRMATION_BEFORE, REPLY_CONFIRMATION_AFTER),
        (REPLY_ICON_CLICK_BEFORE, REPLY_ICON_CLICK_AFTER),
    )
    counts = tuple(
        (content.count(before), content.count(after))
        for before, after in replacements
    )
    if all(before_count == 0 and after_count == 1 for before_count, after_count in counts):
        return False
    if any(before_count != 1 or after_count != 0 for before_count, after_count in counts):
        raise RuntimeError(
            "Pinned x-use reply handler no longer matches the audited patch input"
        )
    for before, after in replacements:
        content = content.replace(before, after)
    target.write_text(content, encoding="utf-8")
    return True


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_x_use.py <x-use-source-root>")
    patch_x_use(Path(sys.argv[1]))
