"""
Ollama-powered AI Test Generator.

Given a URL, this module:
  1. Fetches the page HTML (lightweight, no browser needed)
  2. Extracts the structure — forms, inputs, buttons, links — without an LLM
  3. Feeds ONLY the structure (not the full HTML) to Ollama
  4. Parses the LLM's response into validated IR TestCases

Why feed structure not raw HTML?
  Raw HTML of a real page is 50-200KB. A local 7B model has a ~4K token
  context. Feeding raw HTML = garbage in, garbage out. Extracting structure
  first (form fields, button labels, headings) reduces to ~500 tokens —
  comfortably within context — and the LLM produces dramatically better tests.

Supported models (set in OllamaAIGenerator constructor):
  qwen2.5-coder:7b   — best code/test quality, needs ~8GB RAM (recommended)
  llama3.2:3b        — fastest, works on any machine, ~4GB RAM
  mistral:7b         — good general quality
  codellama:7b       — alternative code model

Install Ollama: https://ollama.com/download
Then: ollama pull qwen2.5-coder:7b
"""
from __future__ import annotations

import json
import re
import urllib.request
import urllib.error
from html.parser import HTMLParser
from typing import List, Optional, Dict, Any

from taas.ir.schema import (
    TestCase, TestStep, TestCategory, ActionType, Locator
)
from taas.ir.validator import IRValidator


# ---- lightweight HTML structure extractor (no dependencies) ----------
class _StructureExtractor(HTMLParser):
    """Pulls forms, inputs, buttons, headings from HTML. No BeautifulSoup needed."""

    def __init__(self):
        super().__init__()
        self.forms: List[Dict] = []
        self.buttons: List[Dict] = []
        self.headings: List[str] = []
        self.links: List[Dict] = []
        self._cur_form: Optional[Dict] = None
        self._capture: Optional[str] = None
        self._capture_buf = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self._cur_form = {
                "action": a.get("action", ""),
                "method": a.get("method", "get"),
                "inputs": [],
            }
        elif tag == "input":
            inp = {
                "type": a.get("type", "text"),
                "id": a.get("id", ""),
                "name": a.get("name", ""),
                "placeholder": a.get("placeholder", ""),
            }
            if self._cur_form is not None:
                self._cur_form["inputs"].append(inp)
        elif tag == "button" or (tag == "input" and a.get("type") in ("submit", "button")):
            self.buttons.append({
                "type": a.get("type", "button"),
                "id": a.get("id", ""),
                "text": a.get("value", ""),
            })
        elif tag in ("h1", "h2", "h3"):
            self._capture = tag
            self._capture_buf = ""
        elif tag == "a":
            link = {"href": a.get("href", ""), "id": a.get("id", ""), "text": ""}
            self.links.append(link)
            self._capture = "a"
            self._capture_buf = ""

    def handle_endtag(self, tag):
        if tag == "form" and self._cur_form is not None:
            self.forms.append(self._cur_form)
            self._cur_form = None
        if tag in ("h1", "h2", "h3") and self._capture:
            self.headings.append(self._capture_buf.strip())
            self._capture = None
        if tag == "a" and self._capture == "a":
            if self.links:
                self.links[-1]["text"] = self._capture_buf.strip()[:60]
            self._capture = None

    def handle_data(self, data):
        if self._capture is not None:
            self._capture_buf += data

    def summary(self) -> str:
        """Compact text description fed to the LLM — ~300-500 tokens."""
        lines = []
        if self.headings:
            lines.append(f"Page headings: {', '.join(self.headings[:5])}")
        for i, form in enumerate(self.forms):
            lines.append(f"Form {i+1}: action={form['action']} method={form['method']}")
            for inp in form["inputs"]:
                lines.append(
                    f"  input: type={inp['type']} id={inp['id']!r} "
                    f"name={inp['name']!r} placeholder={inp['placeholder']!r}"
                )
        for btn in self.buttons[:5]:
            lines.append(f"Button: id={btn['id']!r} text={btn['text']!r}")
        if self.links:
            hrefs = [l["href"] for l in self.links if l["href"] and not l["href"].startswith("#")]
            lines.append(f"Links: {', '.join(hrefs[:8])}")
        return "\n".join(lines) if lines else "No form structure detected."


def _fetch_page_structure(url: str, timeout: int = 10) -> str:
    """Fetch a URL and return a compact structural description."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "TaaS-Bot/1.0 (test-generation-agent)"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read(80_000).decode("utf-8", errors="ignore")
    except Exception as e:
        raise RuntimeError(f"Could not fetch {url}: {e}")
    extractor = _StructureExtractor()
    extractor.feed(html)
    return extractor.summary()


# ---- prompt builder --------------------------------------------------
def _build_prompt(url: str, structure: str) -> str:
    return f"""You are a senior QA engineer. Given a web page structure, generate comprehensive test cases.

URL: {url}
Page structure:
{structure}

Generate test cases covering:
1. Happy path (valid inputs, successful flow)
2. Negative tests (wrong credentials, invalid data)
3. Edge cases (empty fields, boundary values)
4. Security (injection probes, must NOT bypass auth)

Return ONLY a JSON array. Each item must have exactly these fields:
{{
  "name": "descriptive test name",
  "category": "happy_path" | "negative" | "edge_case" | "security",
  "steps": [
    {{
      "action": "navigate" | "fill" | "click" | "assert_text" | "assert_visible" | "assert_url" | "screenshot" | "security_scan",
      "locator": {{"strategy": "id"|"name"|"css"|"xpath", "value": "selector"}} or null,
      "value": "text to type or expected text or url path" or null,
      "description": "what this step does"
    }}
  ]
}}

Rules:
- Use real element IDs/names from the structure above
- Every test must end with at least one assert step
- Security tests must assert the injection DID NOT succeed
- Return ONLY the JSON array, no explanation, no markdown fences"""


# ---- response parser -------------------------------------------------
def _parse_llm_response(raw: str, source: str) -> List[TestCase]:
    """Parse LLM JSON output into validated TestCases. Fails loudly on garbage."""
    # Strip markdown fences the model sometimes adds despite instructions
    clean = re.sub(r"```(?:json)?", "", raw).strip()
    # Find the JSON array even if the model added preamble
    match = re.search(r"\[.*\]", clean, re.DOTALL)
    if not match:
        raise ValueError(
            "LLM did not return a JSON array. "
            f"Raw response (first 300 chars): {raw[:300]}"
        )
    data = json.loads(match.group(0))

    # Validate before constructing models
    validator = IRValidator()
    result = validator.validate_suite(data)
    if not result.ok:
        issues = "; ".join(f"{i.path}: {i.message}" for i in result.errors)
        raise ValueError(f"LLM output failed IR validation: {issues}")

    cases: List[TestCase] = []
    for item in data:
        item["source"] = source
        # Reconstruct step objects
        steps = []
        for s in item.get("steps", []):
            loc = s.get("locator")
            locator = Locator(**loc) if isinstance(loc, dict) and loc else None
            steps.append(TestStep(
                action=ActionType(s["action"]),
                locator=locator,
                value=s.get("value"),
                description=s.get("description"),
            ))
        item["steps"] = steps
        cases.append(TestCase(**{k: v for k, v in item.items()
                                 if k in TestCase.model_fields}))
    return cases


# ---- main class ------------------------------------------------------
class OllamaAIGenerator:
    """
    Generate test cases for any URL using a local Ollama model.

    Usage:
        gen = OllamaAIGenerator()          # uses qwen2.5-coder:7b
        cases = gen.generate_for_url("https://example.com/login")

    Requirements:
        1. Install Ollama: https://ollama.com/download
        2. Pull a model: ollama pull qwen2.5-coder:7b
        3. Make sure Ollama is running (it starts automatically on Windows)
    """

    def __init__(
        self,
        model: str = "qwen2.5-coder:7b",
        host: str = "http://localhost:11434",
        timeout: int = 300,
    ):
        self.model = model
        self.host = host
        self.timeout = timeout

    def is_available(self) -> bool:
        """Check if Ollama is running and the model is pulled."""
        try:
            req = urllib.request.Request(f"{self.host}/api/tags")
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.loads(r.read())
            models = [m["name"].split(":")[0] for m in data.get("models", [])]
            return self.model.split(":")[0] in models
        except Exception:
            return False

    def list_models(self) -> List[str]:
        """Return models currently pulled in Ollama."""
        try:
            req = urllib.request.Request(f"{self.host}/api/tags")
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.loads(r.read())
            return [m["name"] for m in data.get("models", [])]
        except Exception:
            return []

    def generate_for_url(self, url: str, structure: str = None) -> List[TestCase]:
        """
        Main entry point. Fetches the page (through a real browser when
        possible), extracts structure, calls Ollama, returns validated IR
        TestCases. A pre-computed structure string can be passed in to avoid
        fetching the page twice.
        """
        source = f"ai:ollama:{url}"

        # 1. Use the rendered-page structure when available so Ollama sees the
        #    REAL elements and doesn't hallucinate selectors. Fall back to the
        #    plain fetch only if the rendered read produced nothing.
        if structure is None:
            try:
                from taas.ai.smart_url import extract_structure
                s = extract_structure(url)   # reads through real browser first
                structure = s.get("summary") or _fetch_page_structure(url)
            except Exception:
                structure = _fetch_page_structure(url)

        # 2. Build prompt and call Ollama
        prompt = _build_prompt(url, structure)
        raw_response = self._call_ollama(prompt)

        # 3. Parse and validate
        return _parse_llm_response(raw_response, source)

    def _call_ollama(self, prompt: str) -> str:
        payload = json.dumps({
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0.1,   # low temp = more deterministic, fewer hallucinations
                "num_predict": 2048,
            },
        }).encode()
        req = urllib.request.Request(
            f"{self.host}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read())["response"]
        except urllib.error.URLError:
            raise RuntimeError(
                f"Cannot reach Ollama at {self.host}. "
                "Make sure Ollama is installed and running. "
                "Download: https://ollama.com/download"
            )
