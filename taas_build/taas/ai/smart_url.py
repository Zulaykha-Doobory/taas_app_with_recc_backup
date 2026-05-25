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


def extract_structure(url: str) -> Dict[str, Any]:
    """Return forms/inputs/buttons/headings/links for a URL as plain dicts."""
    html = _fetch_raw(url)
    ex = _StructureExtractor()
    ex.feed(html)
    return {
        "forms": ex.forms,
        "buttons": ex.buttons,
        "headings": ex.headings,
        "links": [l for l in ex.links if l.get("href")],
        "summary": ex.summary(),
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


class SmartURLGenerator:
    """
    Generates IR test cases for any URL from its detected structure.
    Uses Ollama automatically if available, else a deterministic builder.
    """

    def __init__(self, use_ollama_if_available: bool = True):
        self.use_ollama = use_ollama_if_available

    def generate_suite(self, url: str, suite_name: Optional[str] = None) -> TestSuite:
        struct = extract_structure(url)

        # Try Ollama first if requested and available
        cases: List[TestCase] = []
        used = "structure"
        if self.use_ollama:
            try:
                from taas.ai.ollama_generator import OllamaAIGenerator
                gen = OllamaAIGenerator()
                if gen.is_available():
                    cases = gen.generate_for_url(url)
                    used = "ollama"
            except Exception:
                cases = []

        # Deterministic structure-based generation (always works)
        if not cases:
            cases = self._from_structure(url, struct)
            used = "structure"

        suite = TestSuite(
            suite_name=suite_name or f"Auto: {url}",
            base_url=url,
            cases=cases,
            metadata={"generated_by": used, "structure": struct["summary"]},
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
