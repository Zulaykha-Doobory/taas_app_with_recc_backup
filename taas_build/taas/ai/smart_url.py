"""
Smart URL -> Test generator.

Given ANY url, this:
  1. fetches the page and extracts its structure (forms, inputs, buttons)
  2. generates real IR TestCases from that structure  -- works with NO Ollama
  3. if Ollama IS running, uses it for richer tests automatically

This is what powers "paste any URL and it tests it". The structure-based
generator is deterministic and always available; Ollama is a bonus.
"""
from __future__ import annotations

from typing import List, Dict, Any, Optional

from taas.ir.schema import (
    TestCase, TestStep, TestCategory, ActionType, Locator, TestSuite
)
from taas.ai.ollama_generator import _StructureExtractor, _fetch_page_structure
import urllib.request


def _fetch_raw(url: str, timeout: int = 10) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "TaaS-Bot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(120_000).decode("utf-8", errors="ignore")


def _fetch_rendered(url: str, timeout: int = 20) -> Optional[str]:
    """
    Load the page in a REAL headless Chrome and return the rendered HTML
    (after JavaScript runs). This sees what a real user's browser sees, so it
    works on JS-heavy single-page apps and gets past most bot blocks that
    reject plain requests. Returns None if Selenium/Chrome isn't available.
    """
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        import time as _t

        opts = Options()
        opts.add_argument("--headless=new")
        opts.add_argument("--window-size=1280,900")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        # a real browser UA helps with sites that sniff for bots
        opts.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0 Safari/537.36")
        opts.add_experimental_option("excludeSwitches", ["enable-logging"])

        driver = None
        try:
            driver = webdriver.Chrome(options=opts)
        except Exception:
            from selenium.webdriver.chrome.service import Service
            from webdriver_manager.chrome import ChromeDriverManager
            driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()),
                                      options=opts)
        try:
            driver.set_page_load_timeout(timeout)
            driver.get(url)
            _t.sleep(2)  # let JS render
            return driver.page_source
        finally:
            try:
                driver.quit()
            except Exception:
                pass
    except Exception:
        return None


def extract_structure(url: str, prefer_browser: bool = True) -> Dict[str, Any]:
    """
    Return forms/inputs/buttons/headings/links for a URL as plain dicts.

    By default reads the page through a REAL browser (so JS-rendered and
    bot-protected sites work). Falls back to a plain HTTP fetch if the browser
    isn't available or fails.
    """
    html = None
    read_via = "fetch"
    if prefer_browser:
        html = _fetch_rendered(url)
        if html:
            read_via = "browser"
    if not html:
        html = _fetch_raw(url)
        read_via = "fetch"

    ex = _StructureExtractor()
    ex.feed(html)
    return {
        "forms": ex.forms,
        "buttons": ex.buttons,
        "headings": ex.headings,
        "links": [l for l in ex.links if l.get("href")],
        "summary": ex.summary(),
        "read_via": read_via,
    }


def _best_locator(inp: Dict[str, str]) -> Optional[Locator]:
    """Pick the most reliable locator for an input element."""
    if inp.get("id"):
        return Locator(strategy="id", value=inp["id"])
    if inp.get("name"):
        return Locator(strategy="name", value=inp["name"])
    if inp.get("type"):
        return Locator(strategy="css", value=f"input[type={inp['type']}]")
    return None


def _sample_value(inp: Dict[str, str]) -> str:
    """A reasonable value to type into a field based on its type/name."""
    t = (inp.get("type") or "text").lower()
    name = (inp.get("name") or inp.get("id") or "").lower()
    if t == "email" or "email" in name:
        return "test@example.com"
    if t == "password" or "pass" in name:
        return "Password123!"
    if t == "tel" or "phone" in name:
        return "1234567890"
    if t == "number":
        return "42"
    if "user" in name or "login" in name:
        return "testuser"
    if "search" in name:
        return "test query"
    return "Test Input"


def _collect_real_identifiers(struct: Dict[str, Any]) -> set:
    """Gather every id/name/link-text that actually exists on the page."""
    ids = set()
    for form in struct.get("forms", []):
        for inp in form.get("inputs", []):
            if inp.get("id"):
                ids.add(inp["id"].lower())
            if inp.get("name"):
                ids.add(inp["name"].lower())
    for b in struct.get("buttons", []):
        if b.get("id"):
            ids.add(b["id"].lower())
        if b.get("text"):
            ids.add(b["text"].lower())
    for l in struct.get("links", []):
        if l.get("id"):
            ids.add(l["id"].lower())
        if l.get("text"):
            ids.add(l["text"].lower())
    return ids


def _filter_to_real_elements(cases: List[TestCase], struct: Dict[str, Any]) -> List[TestCase]:
    """
    Remove test steps whose id/name locator points at an element that isn't on
    the real page. Steps using css/xpath or assertions are left alone (they may
    be legitimately general). A case with no usable steps left is dropped.
    """
    real = _collect_real_identifiers(struct)
    if not real:
        # We couldn't read structure (e.g. blocked page) -> don't over-filter.
        return cases

    kept = []
    for c in cases:
        good_steps = []
        for s in c.steps:
            loc = s.locator
            if loc and loc.strategy in ("id", "name") and loc.value:
                if loc.value.lower() not in real:
                    # references an element that doesn't exist -> skip step
                    continue
            if loc and loc.strategy == "link_text" and loc.value:
                if loc.value.lower() not in real:
                    continue
            good_steps.append(s)
        # keep the case only if it still has a meaningful action left
        if any(st.action.value not in ("navigate",) for st in good_steps):
            c.steps = good_steps
            kept.append(c)
    return kept


class SmartURLGenerator:
    """
    Generates IR test cases for any URL from its detected structure.
    Uses Ollama automatically if available, else a deterministic builder.
    """

    def __init__(self, use_ollama_if_available: bool = True):
        self.use_ollama = use_ollama_if_available

    def generate_suite(self, url: str, suite_name: Optional[str] = None) -> TestSuite:
        struct = extract_structure(url)

        # ALWAYS build the structure tests first -- this is the reliable baseline
        # so you never end up with too few tests if the AI is slow or returns little.
        structure_cases = self._from_structure(url, struct)

        ai_cases: List[TestCase] = []
        if self.use_ollama:
            try:
                from taas.ai.ollama_generator import OllamaAIGenerator
                gen = OllamaAIGenerator()
                if gen.is_available():
                    ai_cases = gen.generate_for_url(url, structure=struct.get("summary"))
            except Exception:
                ai_cases = []

        # Drop AI steps that reference elements which DON'T exist on the real
        # rendered page. This kills hallucinated selectors (e.g. a model
        # inventing 'nav-assist-show-shortcuts' that isn't on the page).
        ai_cases = _filter_to_real_elements(ai_cases, struct)

        # Detect an unreadable page: no forms, no buttons, no links means the
        # site blocked our browser (common on bot-protected sites). In that
        # case, element-specific AI tests are pure guesses, so we drop any that
        # use id/name/link locators and keep only safe page-level checks.
        page_readable = bool(struct.get("forms") or struct.get("buttons")
                             or struct.get("links"))
        page_note = None
        if not page_readable:
            page_note = ("This page could not be fully read (it likely blocks "
                         "automated browsers). Generated only basic page-level "
                         "checks; element-specific tests need a readable page.")
            def _safe(c):
                c.steps = [s for s in c.steps
                           if not (s.locator and s.locator.strategy in
                                   ("id", "name", "link_text"))]
                return c
            ai_cases = [_safe(c) for c in ai_cases
                        if any(st.action.value not in ("navigate",)
                               for st in _safe(c).steps)]
            structure_cases = [_safe(c) for c in structure_cases
                               if any(st.action.value not in ("navigate",)
                                      for st in _safe(c).steps)]

        # Combine: structure baseline + any AI-generated extras.
        # De-dupe by test name so we don't repeat the same check.
        seen = set()
        cases: List[TestCase] = []
        for c in ai_cases + structure_cases:
            key = c.name.strip().lower()
            if key not in seen:
                seen.add(key)
                cases.append(c)

        if ai_cases and structure_cases:
            used = "ollama + structure"
        elif ai_cases:
            used = "ollama"
        else:
            used = "structure"

        suite = TestSuite(
            suite_name=suite_name or f"Auto: {url}",
            base_url=url,
            cases=cases,
            metadata={"generated_by": used, "structure": struct["summary"],
                      "read_via": struct.get("read_via", "fetch"),
                      "page_note": page_note,
                      "ai_count": len(ai_cases), "structure_count": len(structure_cases)},
        )
        return suite

    def _from_structure(self, url: str, struct: Dict[str, Any]) -> List[TestCase]:
        """Build sensible tests purely from detected page structure."""
        cases: List[TestCase] = []

        # 1. Always: page loads and a heading/landmark is present
        load_steps = [TestStep(action=ActionType.NAVIGATE, value=url,
                               description=f"Open {url}")]
        if struct["headings"]:
            load_steps.append(TestStep(
                action=ActionType.ASSERT_TEXT,
                locator=Locator(strategy="css", value="body"),
                value=struct["headings"][0][:40],
                description="Page shows its main heading"))
        else:
            load_steps.append(TestStep(
                action=ActionType.ASSERT_VISIBLE,
                locator=Locator(strategy="css", value="body"),
                description="Page body is visible"))
        cases.append(TestCase(name="Page loads successfully",
                              category=TestCategory.HAPPY_PATH,
                              source=f"auto:{url}", steps=load_steps))

        # 2. For each form: a happy-path fill+submit, plus an empty-submit negative
        for fi, form in enumerate(struct["forms"]):
            fillable = [i for i in form["inputs"]
                        if (i.get("type") or "text") not in ("hidden", "submit", "button")]
            if not fillable:
                continue

            # happy path: fill every field, then submit
            happy = [TestStep(action=ActionType.NAVIGATE, value=url,
                              description=f"Open {url}")]
            for inp in fillable:
                loc = _best_locator(inp)
                if loc:
                    happy.append(TestStep(
                        action=ActionType.FILL, locator=loc,
                        value=_sample_value(inp),
                        description=f"Fill {inp.get('name') or inp.get('id') or inp.get('type')}"))
            submit_loc = self._submit_locator(struct)
            if submit_loc:
                happy.append(TestStep(action=ActionType.CLICK, locator=submit_loc,
                                      description="Submit the form"))
            cases.append(TestCase(
                name=f"Form {fi+1}: submit with valid data",
                category=TestCategory.HAPPY_PATH,
                source=f"auto:{url}", steps=happy))

            # negative: submit empty
            if submit_loc:
                neg = [
                    TestStep(action=ActionType.NAVIGATE, value=url,
                             description=f"Open {url}"),
                    TestStep(action=ActionType.CLICK, locator=submit_loc,
                             description="Submit without filling anything"),
                    TestStep(action=ActionType.ASSERT_VISIBLE,
                             locator=Locator(strategy="css", value="body"),
                             description="Page still responds (validation expected)"),
                ]
                cases.append(TestCase(
                    name=f"Form {fi+1}: submit empty (negative)",
                    category=TestCategory.NEGATIVE,
                    source=f"auto:{url}", steps=neg))

        # 3. If there are links, check the first internal one is reachable
        internal = [l for l in struct["links"]
                    if l["href"].startswith("/") or url.split("//")[-1].split("/")[0] in l["href"]]
        if internal:
            href = internal[0]["href"]
            cases.append(TestCase(
                name="Primary navigation link works",
                category=TestCategory.HAPPY_PATH,
                source=f"auto:{url}",
                steps=[
                    TestStep(action=ActionType.NAVIGATE, value=url,
                             description=f"Open {url}"),
                    TestStep(action=ActionType.NAVIGATE, value=href,
                             description=f"Follow link to {href}"),
                    TestStep(action=ActionType.ASSERT_VISIBLE,
                             locator=Locator(strategy="css", value="body"),
                             description="Destination page loads"),
                ]))

        return cases

    @staticmethod
    def _submit_locator(struct: Dict[str, Any]) -> Optional[Locator]:
        """Find a submit button locator from the page structure."""
        for btn in struct["buttons"]:
            if btn.get("id"):
                return Locator(strategy="id", value=btn["id"])
            if btn.get("type") == "submit":
                return Locator(strategy="css", value="button[type=submit], input[type=submit]")
        # fallback to a generic submit selector
        if struct["buttons"]:
            return Locator(strategy="css", value="button, input[type=submit]")
        return None
