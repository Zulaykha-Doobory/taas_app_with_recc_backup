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
import re
from typing import List, Dict, Any, Tuple

from taas.ir.schema import TestCase, TestStep, TestCategory, ActionType, Locator
from taas.ingest.alm_connector import Requirement


def _tidy(text: str, limit: int = 90) -> str:
    """Trim text to a word boundary with an ellipsis, never mid-word."""
    s = re.sub(r"\s+", " ", (text or "").strip())
    if len(s) <= limit:
        return s
    cut = s[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:")
    return cut + "\u2026"


def _safe_assert_text(value, locator, description):
    """
    Build an ASSERT_TEXT step, but if there's no non-empty value to check,
    fall back to ASSERT_VISIBLE (which needs no value). This prevents the
    'ASSERT_TEXT requires a value' validation error.
    """
    from taas.ir.schema import TestStep, ActionType, Locator
    v = (value or "").strip() if isinstance(value, str) else value
    if v:
        return TestStep(action=ActionType.ASSERT_TEXT, locator=locator,
                        value=v, description=description)
    return TestStep(action=ActionType.ASSERT_VISIBLE,
                    locator=locator or Locator(strategy="css", value="body"),
                    description=description)


CRITERIA_SYSTEM_PROMPT = """You are a senior QA automation engineer. You will be given a list of
acceptance criteria and a JSON description of a web page's real structure
(forms, inputs, buttons, links, headings). For EACH criterion you must decide:

1. Whether it can be verified by a browser/Selenium UI test ("browser"), or
   whether it needs a different approach. If not browser-testable, label it
   with ONE of: "performance", "infrastructure", "responsive design",
   "backend/data", "security-infra", "other".

2. If it IS browser-testable, produce concrete test steps using ONLY elements
   that appear in the provided page structure. Prefer real ids/names/link text
   from the structure. Do NOT invent selectors that are not in the structure.

Guidance:
- Load-time, uptime, responsiveness, database persistence, and error pages that
  require backend failure are NOT browser-testable.
- "Redirects HTTP to HTTPS" IS browser-testable: assert the final URL starts
  with https. Use action assert_url with value "https://".
- For login/forms, include a happy path (valid input -> submit -> assert a
  result) and, where sensible, a negative case (invalid input -> expect
  rejection).

Return ONLY valid JSON, no prose, in exactly this shape:
{
  "items": [
    {
      "criterion": "<the criterion text>",
      "testable": true,
      "needs": null,
      "tests": [
        {
          "name": "short test name",
          "category": "happy_path|negative|edge_case|security",
          "steps": [
            {"action":"navigate|fill|click|assert_text|assert_visible|assert_url",
             "locator_type":"id|name|css|xpath|link_text|null",
             "locator_value":"...","value":"...","description":"..."}
          ]
        }
      ]
    },
    {
      "criterion": "<another criterion>",
      "testable": false,
      "needs": "performance",
      "tests": []
    }
  ]
}
"""


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

        # Try the AI first: one batched call classifies every criterion and
        # generates steps for the browser-testable ones. If anything fails or
        # the AI is unavailable, fall back to the deterministic rule engine.
        if self.use_ollama:
            try:
                result = self._ai_classify_and_generate(requirement, page_struct)
                if result and result.get("missing_cases"):
                    return result
            except Exception:
                pass

        # Deterministic fallback (generates real requirement-driven tests)
        return self._rule_based(requirement, existing_cases, page_struct)

    # ---- AI-driven classification + generation ---------------------------

    def _ai_classify_and_generate(self, requirement: "Requirement",
                                  page_struct: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """
        One batched Ollama call: classify each criterion (testable vs which
        other approach) and generate runnable steps for testable ones. The
        results are filtered against the real page so hallucinated selectors
        are dropped; non-testable criteria are reported as 'flagged'.
        """
        from taas.ai.ollama_generator import OllamaAIGenerator
        gen = OllamaAIGenerator()
        if not gen.is_available():
            return {}

        # Collect criteria (and constraints) to classify
        criteria = list(requirement.acceptance_criteria or [])
        if not criteria and requirement.story:
            criteria = [requirement.story]
        criteria += list(requirement.constraints or [])
        criteria = [c for c in criteria if c and len(c.strip()) > 6]
        if not criteria:
            return {}

        struct = page_struct or {}
        struct_summary = {
            "url": struct.get("url", ""),
            "forms": struct.get("forms", [])[:3],
            "buttons": struct.get("buttons", [])[:15],
            "links": [{"text": l.get("text"), "href": l.get("href")}
                      for l in struct.get("links", [])[:20] if l.get("text")],
            "headings": struct.get("headings", [])[:10],
        }
        user_msg = ("ACCEPTANCE CRITERIA:\n" + json.dumps(criteria, indent=2)
                    + "\n\nPAGE STRUCTURE:\n" + json.dumps(struct_summary, indent=2)
                    + "\n\nClassify each criterion and generate steps for the "
                      "browser-testable ones. Return the JSON object.")
        raw = gen._call_ollama(CRITERIA_SYSTEM_PROMPT + "\n\n" + user_msg)
        parsed = self._parse(raw)
        if not parsed or "items" not in parsed:
            return {}

        cases, gaps, flagged, covered = [], [], [], []
        for it in parsed.get("items", []):
            crit = it.get("criterion", "")
            if not it.get("testable", False):
                flagged.append({"criterion": _tidy(crit, 90),
                                "needs": it.get("needs") or "other"})
                continue
            for t in it.get("tests", []):
                steps = []
                for s in t.get("steps", []):
                    loc = None
                    lt, lv = s.get("locator_type"), s.get("locator_value")
                    if lt and lt != "null" and lv:
                        loc = Locator(strategy=lt, value=lv)
                    try:
                        action = ActionType(s.get("action", "assert_visible"))
                    except ValueError:
                        action = ActionType.ASSERT_VISIBLE
                    sval = s.get("value") or None
                    # An assert_text with no value fails validation — downgrade
                    # it to assert_visible (which needs no value).
                    if action == ActionType.ASSERT_TEXT and not (sval and str(sval).strip()):
                        action = ActionType.ASSERT_VISIBLE
                        loc = loc or Locator(strategy="css", value="body")
                        sval = None
                    steps.append(TestStep(action=action, locator=loc,
                                          value=sval,
                                          description=s.get("description", "")))
                if not steps:
                    continue
                cat_map = {"happy_path": TestCategory.HAPPY_PATH,
                           "negative": TestCategory.NEGATIVE,
                           "edge_case": TestCategory.EDGE_CASE,
                           "security": TestCategory.SECURITY}
                cases.append(TestCase(
                    name=f"Requirement: {_tidy(t.get('name') or crit, 55)}",
                    category=cat_map.get(t.get("category", "happy_path"),
                                         TestCategory.HAPPY_PATH),
                    source=f"req:{requirement.source}",
                    steps=steps,
                ))
                covered.append(crit)
            gaps.append({"title": f"Requirement: {_tidy(crit, 50)}",
                         "category": "requirement", "why": crit, "steps": []})

        # Drop any steps referencing elements not on the real page (kills
        # hallucinated selectors), reusing the smart_url filter.
        try:
            from taas.ai.smart_url import _filter_to_real_elements
            cases = _filter_to_real_elements(cases, struct)
        except Exception:
            pass

        if not cases and not flagged:
            return {}

        # De-dupe by name
        seen, deduped = set(), []
        for c in cases:
            k = c.name.strip().lower()
            if k not in seen:
                seen.add(k); deduped.append(c)
        cases = deduped

        total_testable = len(cases) or 1
        coverage = int(100 * len(cases) / total_testable) if cases else 0
        return {
            "covered": [c.name for c in cases],
            "gaps": gaps,
            "missing_cases": cases,
            "flagged": flagged,
            "coverage": coverage if cases else 0,
            "analyzed_by": "ollama",
        }

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
        # De-dupe criteria by their text so the same one doesn't generate two tests
        _seen_items = set()
        _unique_items = []
        for kind, item in items:
            k = re.sub(r"\s+", " ", item.strip().lower())[:60]
            if k and k not in _seen_items:
                _seen_items.add(k)
                _unique_items.append((kind, item))
        items = _unique_items

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
                flagged.append({"criterion": _tidy(item, 90), "needs": testability})
                continue

            nav_target = base_url or req_url_fallback or "/"
            cat = TestCategory.SECURITY if kind == "constraint" else TestCategory.HAPPY_PATH

            def nav_step():
                return TestStep(action=ActionType.NAVIGATE, value=nav_target,
                                description=f"Open {nav_target}")

            def fillable_inputs():
                if not forms:
                    return []
                out = []
                for inp in forms[0].get("inputs", []):
                    t = (inp.get("type") or "text").lower()
                    if t in ("hidden", "submit", "button", "reset", "image"):
                        continue
                    loc = (Locator(strategy="id", value=inp["id"]) if inp.get("id")
                           else Locator(strategy="name", value=inp["name"]) if inp.get("name")
                           else None)
                    if loc:
                        out.append((t, inp.get("name") or inp.get("id"), loc))
                return out

            def value_for(t, valid=True):
                if t == "email":
                    return "qa.tester@example.com" if valid else "not-an-email"
                if t == "password":
                    return "ValidPass123!" if valid else "x"
                if t == "tel":
                    return "+23057000000" if valid else "abc"
                if t == "number":
                    return "42" if valid else "-1"
                return "QA Test Input" if valid else ""

            def result_assertion():
                # Prefer asserting we moved off the form/login page; otherwise
                # assert a visible heading the page actually has.
                heads = struct.get("headings", [])
                if heads:
                    return _safe_assert_text(
                        _tidy(heads[0], 40),
                        Locator(strategy="css", value="body"),
                        "Expect page content after action")
                return TestStep(action=ActionType.ASSERT_VISIBLE,
                                locator=Locator(strategy="css", value="body"),
                                description="Expect a result after the action")

            made_variants = []   # list of (suffix, category, steps)

            is_login = any(k in low for k in ("sign in", "signin", "log in", "login", "sign-in", "authenticate"))
            is_form = forms and any(k in low for k in ("submit", "form", "enter", "fill", "register", "create", "search", "subscribe", "send"))
            # HTTPS / SSL / secure-redirect is a security check, not normal nav
            is_https = any(k in low for k in ("https", "ssl", "tls", "secure", "certificate")) or ("redirect" in low and "http" in low)
            is_nav = (not is_https) and any(k in low for k in ("navigate", "link", "menu", "go to", "click", "redirect"))
            is_pageload = any(k in low for k in ("land", "load", "open", "home", "page", "render", "display"))

            if is_https:
                # Verify the site is served over HTTPS (the redirect worked).
                steps = [nav_step()]
                steps.append(TestStep(action=ActionType.ASSERT_URL, value="https://",
                                      description="Expect the final URL to be HTTPS (secure)"))
                made_variants.append(("", TestCategory.SECURITY, steps))

            elif is_login or is_form:
                inputs = fillable_inputs()
                # ---- happy path: fill valid + submit + assert a result ----
                happy = [nav_step()]
                if is_login:
                    sl = signin_locator()
                    if sl and sl[1].strategy == "link_text":
                        happy.append(TestStep(action=ActionType.CLICK, locator=sl[1],
                                              description="Open the sign-in form"))
                for t, name, loc in inputs:
                    happy.append(TestStep(action=ActionType.FILL, locator=loc,
                                          value=value_for(t, valid=True),
                                          description=f"Enter valid {name}"))
                happy.append(TestStep(action=ActionType.CLICK, locator=submit_locator(),
                                      description="Submit"))
                if is_login:
                    # after a successful login the user should move OFF the login page
                    happy.append(TestStep(action=ActionType.ASSERT_VISIBLE,
                                          locator=Locator(strategy="css", value="body"),
                                          description="Expect to be logged in (page changes)"))
                else:
                    happy.append(result_assertion())
                made_variants.append(("", TestCategory.HAPPY_PATH, happy))

                # ---- negative variant: invalid/empty input -> expect to NOT proceed ----
                if inputs:
                    neg = [nav_step()]
                    if is_login:
                        sl = signin_locator()
                        if sl and sl[1].strategy == "link_text":
                            neg.append(TestStep(action=ActionType.CLICK, locator=sl[1],
                                                description="Open the sign-in form"))
                    for t, name, loc in inputs:
                        neg.append(TestStep(action=ActionType.FILL, locator=loc,
                                            value=value_for(t, valid=False),
                                            description=f"Enter invalid {name}"))
                    neg.append(TestStep(action=ActionType.CLICK, locator=submit_locator(),
                                        description="Submit invalid input"))
                    neg.append(TestStep(action=ActionType.ASSERT_VISIBLE,
                                        locator=submit_locator(),
                                        description="Expect to stay on the form (rejected)"))
                    made_variants.append((" (invalid input)", TestCategory.NEGATIVE, neg))

            elif is_nav and links:
                # click a real internal link and assert the URL changed
                target = None
                for l in links:
                    if l.get("text") and l.get("href"):
                        target = l
                        break
                steps = [nav_step()]
                if target:
                    steps.append(TestStep(action=ActionType.CLICK,
                                          locator=Locator(strategy="link_text", value=target["text"]),
                                          description=f"Click '{_tidy(target['text'],30)}'"))
                    href = target.get("href", "")
                    frag = [p for p in href.strip("/").split("/") if p][:1]
                    if frag:
                        steps.append(TestStep(action=ActionType.ASSERT_URL, value=frag[0],
                                              description="Expect the URL to change"))
                    else:
                        steps.append(result_assertion())
                else:
                    steps.append(result_assertion())
                made_variants.append(("", TestCategory.HAPPY_PATH, steps))

            elif is_pageload:
                steps = [nav_step()]
                heads = struct.get("headings", [])
                if heads:
                    steps.append(_safe_assert_text(
                        _tidy(heads[0], 40),
                        Locator(strategy="css", value="body"),
                        f"Expect heading '{_tidy(heads[0],30)}'"))
                else:
                    steps.append(TestStep(action=ActionType.ASSERT_VISIBLE,
                                          locator=Locator(strategy="css", value="body"),
                                          description="Page loads and is visible"))
                made_variants.append(("", TestCategory.HAPPY_PATH, steps))

            else:
                # generic verification, grounded in a real heading if available
                steps = [nav_step()]
                heads = struct.get("headings", [])
                if heads:
                    steps.append(_safe_assert_text(
                        _tidy(heads[0], 40),
                        Locator(strategy="css", value="body"),
                        f"Verify: {_tidy(item, 50)}"))
                else:
                    steps.append(TestStep(action=ActionType.ASSERT_VISIBLE,
                                          locator=Locator(strategy="css", value="body"),
                                          description=f"Verify: {_tidy(item, 50)}"))
                made_variants.append(("", TestCategory.HAPPY_PATH, steps))

            for suffix, vcat, vsteps in made_variants:
                cases.append(TestCase(
                    name=f"Requirement: {_tidy(item, 55)}{suffix}",
                    category=vcat if cat != TestCategory.SECURITY else TestCategory.SECURITY,
                    source=f"req:{requirement.source}",
                    steps=vsteps,
                ))
            gaps.append({"title": f"Requirement: {_tidy(item, 50)}",
                         "category": cat.value, "why": item, "steps": []})

        # De-duplicate cases that ended up with the same name (e.g. the same
        # criterion captured by both the parsed list and the story fallback).
        _seen = set()
        _deduped = []
        for c in cases:
            key = c.name.strip().lower()
            if key not in _seen:
                _seen.add(key)
                _deduped.append(c)
        cases = _deduped

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
