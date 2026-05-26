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
                existing_cases: List[TestCase]) -> Dict[str, Any]:
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

        # Deterministic fallback
        return self._rule_based(requirement, existing_cases)

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
                    existing_cases: List[TestCase]) -> Dict[str, Any]:
        """
        Deterministic gap finder used when the AI is unavailable.
        Matches acceptance criteria & constraints against existing test names
        by keyword overlap; anything unmatched becomes a gap.
        """
        existing_text = " ".join(
            (c.name + " " + " ".join(s.description or "" for s in c.steps)).lower()
            for c in existing_cases
        )

        covered, gaps = [], []
        items = ([("criterion", c) for c in requirement.acceptance_criteria]
                 + [("constraint", c) for c in requirement.constraints])

        for kind, item in items:
            words = [w for w in item.lower().split() if len(w) > 4]
            hit = sum(1 for w in words if w in existing_text)
            if words and hit >= max(1, len(words) // 3):
                covered.append(item)
            else:
                cat = "security" if kind == "constraint" else "negative"
                gaps.append({
                    "title": f"Verify: {item[:50]}",
                    "category": cat,
                    "why": f"{kind} not matched in existing tests",
                    "steps": [],   # rule-based can't synthesize selectors
                })

        total = len(items) or 1
        coverage = int(100 * len(covered) / total)
        return {
            "covered": covered,
            "gaps": gaps,
            "missing_cases": [],   # no runnable steps without page knowledge
            "coverage": coverage,
            "analyzed_by": "rules",
        }
