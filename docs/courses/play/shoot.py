"""Screenshot litai-rendered HTML views for the film (read-only, local files only).

Run with Playwright in its own environment beside the installed Chrome:
  python shoot.py VIEWS_DIR OUTPUT_DIR
VIEWS_DIR holds `litai render html` output (graph.html, verification-health.html).
The graph canvas is sized independently of its taller grid row, so graph shots are
clipped to the painted canvas height rather than showing an unpainted band.
"""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

PANEL = """e => {
  let node = e;
  while (node && node.getBoundingClientRect().width < 900) node = node.parentElement;
  const panel = node.getBoundingClientRect(), canvas = e.getBoundingClientRect();
  return {x: panel.x, y: panel.y + window.scrollY, width: panel.width,
          height: canvas.height};
}"""


def main():
    views, out = Path(sys.argv[1]), Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(
            viewport={"width": 1600, "height": 2400}, device_scale_factor=2
        )
        page.goto((views / "graph.html").as_uri(), wait_until="networkidle")
        page.wait_for_timeout(2000)
        page.get_by_role("button", name="Fit graph").click()
        page.wait_for_timeout(1200)
        canvas = page.locator("canvas").first
        page.screenshot(
            path=str(out / "graph-overview.png"), clip=canvas.evaluate(PANEL)
        )
        page.locator("select").nth(1).select_option(label="component · hello-component")
        page.wait_for_timeout(800)
        page.get_by_role("button", name="Focus selected").click()
        page.wait_for_timeout(1500)
        page.screenshot(
            path=str(out / "graph-selected.png"), clip=canvas.evaluate(PANEL)
        )
        page.screenshot(
            path=str(out / "graph-page.png"),
            clip={"x": 0, "y": 0, "width": 1600, "height": 900},
        )
        for name in ("verification-health", "lock-health"):
            page.goto((views / f"{name}.html").as_uri(), wait_until="networkidle")
            page.wait_for_timeout(1000)
            page.screenshot(
                path=str(out / f"{name}.png"),
                clip={"x": 0, "y": 0, "width": 1600, "height": 900},
            )
        browser.close()
    print(out)


if __name__ == "__main__":
    main()
