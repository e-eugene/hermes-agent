#!/usr/bin/env python3
"""Apply audited Hermes fixes to the pinned x-use source tree."""

from __future__ import annotations

import sys
from pathlib import Path


REPLY_HANDLER = Path("src/xuse/features/publisher/reply_handler.py")

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
    before_count = content.count(REPLY_ICON_CLICK_BEFORE)
    after_count = content.count(REPLY_ICON_CLICK_AFTER)
    if before_count == 0 and after_count == 1:
        return False
    if before_count != 1 or after_count != 0:
        raise RuntimeError(
            "Pinned x-use reply handler no longer matches the audited patch input"
        )
    target.write_text(
        content.replace(REPLY_ICON_CLICK_BEFORE, REPLY_ICON_CLICK_AFTER),
        encoding="utf-8",
    )
    return True


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_x_use.py <x-use-source-root>")
    patch_x_use(Path(sys.argv[1]))
