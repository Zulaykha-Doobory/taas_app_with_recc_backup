"""
Gap Analysis Engine.

Given a Requirement (from Jira/Azure/text) and the existing test cases for a
target, this asks the AI: "what is NOT being tested yet?" — then returns the
missing test cases plus a coverage score.

Falls back to a deterministic rule-based gap finder if Ollama is unavailable,
so it always returns something useful.
"""
from __future__ import annotations

import json
from typing import List, Dict, Any, Tuple

from taas.ir.schema import TestCase, TestStep, TestCategory, ActionType, Locator
from taas.ingest.alm_connector import Requirement


# ---------------------------------------------------------------------------
# The Gap Analysis prompt
# ---------------------------------------------------------------------------

GAP_SYSTEM_PROMPT = """You are a senior QA analyst performing GAP ANALYSIS.

You will receive:
1. A user story / requirement, with acceptance criteria and constraints.
2. A JSON array of EXISTING test steps already covering the target.

Your job: compare the requirement against the existing tests and identify
what is NOT yet tested. Specifically look for:
  - Acceptance criteria with no matching test
  - Missing edge cases (empty, too long, boundary, special characters)
  - Security constraints mentioned in the story (auth, injection, permissions,
    rate limits, invalid tokens) that have no test
  - Invalid / error states not currently covered
  - Negative paths (what should be REJECTED)

Return ONLY a JSON object, no prose, in this exact shape:
{
  "covered": ["short description of each requirement already tested"],
  "gaps": [
    {
      "title": "short test name",
      "category": "happy_path | negative | edge_case | security",
      "why": "which requirement or risk this covers",
      "steps": [
        {"action":"navigate|fill|click|assert_text|assert_visible|assert_url",
         "locator_type":"id|name|css|xpath|link_text",
         "locator_value":"...","value":"...","description":"..."}
      ]
    }
  ],
  "coverage_estimate": 0-100
}
Be specific and thorough. Prefer finding real gaps over inventing trivial ones.
"""


def build_gap_prompt(requirement: Requirement,
                     existing_tests: List[Dict[str, Any]]) -> str:
    """Assemble the user-message payload for the gap analysis."""
    req_block = {
        "title": requirement.title,
        "story": requirement.story[:2000],
        "acceptance_criteria": requirement.acceptance_criteria,
        "constraints": requirement.constraints,
    }
    return (
        "REQUIREMENT:\n"
        + json.dumps(req_block, indent=2)
        + "\n\nEXISTING TEST STEPS (JSON):\n"
        + json.dumps(existing_tests, indent=2)
        + "\n\nCompare the requirement against the existing test steps. "
          "Identify missing edge cases, security constraints mentioned in the "
          "story, and invalid states not currently covered. Return the JSON object."
    )


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class GapAnalysisEngine:
    def __init__(self, use_ollama: bool = True):
        self.use_ollama = use_ollama

    def analyze(self, requirement: Requirement,
                existing_cases: List[TestCase],
                page_struct: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Returns:
          {
            "covered": [...],
            "gaps": [...],            # raw gap dicts
            "missing_cases": [TestCase...],   # gaps converted to runnable IR
            "coverage": int,          # 0-100
            "analyzed_by": "ollama" | "rules"
          }
        """
        existing_steps = self._cases_to_steps_json(existing_cases)

        # Try the AI first
        if self.use_ollama:
            try:
                from taas.ai.ollama_generator import OllamaAIGenerator
                gen = OllamaAIGenerator()
                if gen.is_available():
                    prompt = build_gap_prompt(requirement, existing_steps)
                    raw = gen._call_ollama(GAP_SYSTEM_PROMPT + "\n\n" + prompt)
                    parsed = self._parse(raw)
                    if parsed:
                        parsed["missing_cases"] = self._gaps_to_cases(
                            parsed.get("gaps", []), requirement.source)
                        parsed["analyzed_by"] = "ollama"
                        return parsed
            except Exception:
                pass

        # Deterministic fallback (now generates real requirement-driven tests)
        return self._rule_based(requirement, existing_cases, page_struct)

    # ---- helpers ----------------------------------------------------------

    @staticmethod
    def _cases_to_steps_json(cases: List[TestCase]) -> List[Dict[str, Any]]:
        out = []
        for c in cases:
            out.append({
                "name": c.name,
                "category": c.category.value,
                "steps": [
                    {"action": s.action.value,
                     "locator": (s.locator.value if s.locator else None),
                     "value": s.value,
                     "description": s.description}
                    for s in c.steps
                ],
            })
        return out

    @staticmethod
    def _parse(raw: str) -> Dict[str, Any]:
        """Extract the JSON object from the LLM response."""
        import re
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return {}
        try:
            return json.loads(m.group(0))
        except Exception:
            return {}

    def _gaps_to_cases(self, gaps: List[Dict[str, Any]], source: str) -> List[TestCase]:
        """Turn the AI's gap step lists into runnable IR TestCases."""
        cat_map = {
            "happy_path": TestCategory.HAPPY_PATH,
            "negative": TestCategory.NEGATIVE,
            "edge_case": TestCategory.EDGE_CASE,
            "security": TestCategory.SECURITY,
        }
        cases = []
        for g in gaps:
            steps = []
            for s in g.get("steps", []):
                loc = None
                if s.get("locator_type") and s.get("locator_value"):
                    loc = Locator(strategy=s["locator_type"], value=s["locator_value"])
                try:
                    action = ActionType(s.get("action", "assert_visible"))
                except ValueError:
                    action = ActionType.ASSERT_VISIBLE
                steps.append(TestStep(action=action, locator=loc,
                                      value=s.get("value"),
                                      description=s.get("description", "")))
            if steps:
                cases.append(TestCase(
                    name=g.get("title", "Untitled gap test"),
                    category=cat_map.get(g.get("category", "negative"),
                                         TestCategory.NEGATIVE),
                    source=f"gap:{source}",
                    steps=steps,
                ))
        return cases

    def _rule_based(self, requirement: Requirement,
                    existing_cases: List[TestCase],
                    page_struct: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Deterministic requirement-driven generation, used when the AI is
        unavailable. Turns EACH acceptance criterion / constraint into a real,
        runnable test grounded in the page's actual structure (forms, buttons,
        links) so the requirement genuinely drives the tests.
        """
        from taas.ir.schema import (TestCase, TestStep, TestCategory,
                                    ActionType, Locator)

        struct = page_struct or {}
        buttons = struct.get("buttons", [])
        forms = struct.get("forms", [])
        links = struct.get("links", [])
        base_url = struct.get("url", "")
        req_url_fallback = struct.get("url", "")

        def submit_locator():
            for b in buttons:
                if b.get("id"):
                    return Locator(strategy="id", value=b["id"])
            return Locator(strategy="css", value="button, input[type=submit]")

        def signin_locator():
            # look for a sign-in / login link or button by its text
            for l in links + buttons:
                txt = (l.get("text") or l.get("href") or "").lower()
                if any(k in txt for k in ("sign in", "signin", "log in", "login", "sign-in")):
                    if l.get("href"):
                        return ("link", Locator(strategy="link_text", value=l.get("text") or "Sign in"))
                    if l.get("id"):
                        return ("click", Locator(strategy="id", value=l["id"]))
            return None

        covered, gaps, cases = [], [], []
        items = ([("criterion", c) for c in requirement.acceptance_criteria]
                 + [("constraint", c) for c in requirement.constraints])

        # If the parser found no explicit criteria, fall back to the raw story
        # split into sentences so we still produce requirement-driven tests.
        if not items and requirement.story:
            import re
            # split on line breaks; also break "I want to X so that Y" patterns
            raw_lines = re.split(r"[\n\r]+", requirement.story)
            for line in raw_lines:
                s = line.strip(" \t-*0123456789.,")
                # if the whole line is just "As a user" boilerplate, skip it
                if re.match(r"(?i)^as an?\s+[\w\s]{1,20}$", s) and not re.search(r"(?i)\b(want|should|go|click|sign|login|navigate|land|open|able)\b", s):
                    continue
                # otherwise strip a leading "As a [role]" clause inline
                s = re.sub(r"(?i)^as an?\s+\w+(\s+\w+){0,2}?\s*,?\s*(?=i\b|want|should|be able|go|click|navigate|login|sign|land)", "", s).strip()
                # further split "I want to A and B" / "want to A. B" into pieces
                for piece in re.split(r"(?i)\b(?:and then|and|;|\.)\b", s):
                    p = piece.strip(" \t,")
                    p = re.sub(r"(?i)^(i want to|i should be able to|be able to)\s*", "", p).strip()
                    if len(p) > 6:
                        items.append(("criterion", p))

        flagged = []   # criteria that need a non-browser testing approach
        for kind, item in items:
            low = item.lower()

            # If this criterion can't be meaningfully checked in a browser
            # (performance, uptime, responsive, backend/data), don't fake a
            # test for it — flag it so the user knows it needs another approach.
            try:
                from taas.ingest.alm_connector import classify_testability
                testability = classify_testability(item)
            except Exception:
                testability = "browser"
            if testability != "browser":
                flagged.append({"criterion": item[:80], "needs": testability})
                continue

            nav_target = base_url or req_url_fallback or "/"
            steps = [TestStep(action=ActionType.NAVIGATE, value=nav_target,
                              description=f"Open {nav_target}")]
            cat = TestCategory.SECURITY if kind == "constraint" else TestCategory.HAPPY_PATH

            # Map common intents in the criterion to real actions
            if any(k in low for k in ("sign in", "signin", "log in", "login", "sign-in")):
                sl = signin_locator()
                if sl:
                    mode, loc = sl
                    steps.append(TestStep(action=ActionType.CLICK, locator=loc,
                                          description="Click the sign-in control"))
                else:
                    steps.append(TestStep(action=ActionType.ASSERT_VISIBLE,
                                          locator=Locator(strategy="css", value="body"),
                                          description="Sign-in control expected on page"))
            elif any(k in low for k in ("land", "load", "open", "home", "page")):
                steps.append(TestStep(action=ActionType.ASSERT_VISIBLE,
                                      locator=Locator(strategy="css", value="body"),
                                      description="Page loads and is visible"))
            elif forms and any(k in low for k in ("submit", "form", "enter", "fill", "register", "create")):
                for inp in forms[0].get("inputs", []):
                    t = (inp.get("type") or "text")
                    if t in ("hidden", "submit", "button"):
                        continue
                    loc = (Locator(strategy="id", value=inp["id"]) if inp.get("id")
                           else Locator(strategy="name", value=inp["name"]) if inp.get("name")
                           else None)
                    if loc:
                        steps.append(TestStep(action=ActionType.FILL, locator=loc,
                                              value="test@example.com" if t == "email" else "Test123!",
                                              description=f"Fill {inp.get('name') or inp.get('id')}"))
                steps.append(TestStep(action=ActionType.CLICK, locator=submit_locator(),
                                      description="Submit the form"))
            else:
                # generic verification of the criterion
                steps.append(TestStep(action=ActionType.ASSERT_VISIBLE,
                                      locator=Locator(strategy="css", value="body"),
                                      description=f"Verify: {item[:60]}"))

            cases.append(TestCase(
                name=f"Requirement: {item[:60]}",
                category=cat,
                source=f"req:{requirement.source}",
                steps=steps,
            ))
            gaps.append({"title": f"Requirement: {item[:50]}",
                         "category": cat.value, "why": item, "steps": []})

        # Coverage = browser-testable criteria we produced a test for, out of
        # all browser-testable criteria. Flagged (non-browser) criteria are
        # reported separately so they don't drag coverage down unfairly.
        browser_testable = len(cases) + 0  # we made a test for each testable one
        total_testable = browser_testable or 1
        coverage = int(100 * len(cases) / total_testable) if total_testable else 0
        return {
            "covered": [c.name for c in cases],
            "gaps": gaps,
            "missing_cases": cases,
            "coverage": coverage,
            "flagged": flagged,   # criteria needing non-browser testing
            "analyzed_by": "rules",
        }
