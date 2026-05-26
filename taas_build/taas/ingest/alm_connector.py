"""
ALM Connector — fetches requirements from Jira or Azure DevOps.

SECURITY: All credentials come from environment variables, never hardcoded.
Set these in a .env file (and add .env to .gitignore) or your shell:

  Jira:
    JIRA_BASE   = https://yourcompany.atlassian.net
    JIRA_EMAIL  = you@company.com
    JIRA_PAT    = <your Atlassian API token>

  Azure DevOps:
    AZURE_ORG     = your-org
    AZURE_PROJECT = your-project
    AZURE_PAT     = <your Azure DevOps PAT>

A "Requirement" is the normalized shape both sources are parsed into, so the
gap-analysis engine doesn't care where it came from.
"""
from __future__ import annotations

import os
import re
import json
import base64
import urllib.request
import urllib.error
from dataclasses import dataclass, field, asdict
from typing import List, Optional


@dataclass
class Requirement:
    """Normalized requirement, independent of Jira/Azure specifics."""
    source: str                       # "jira:PROJ-123" or "azure:456" or "text"
    title: str
    story: str                        # the main description / user story body
    acceptance_criteria: List[str] = field(default_factory=list)
    constraints: List[str] = field(default_factory=list)   # security/validation notes
    raw_url: Optional[str] = None     # link back to the ticket

    def to_dict(self):
        return asdict(self)


# ---------------------------------------------------------------------------
# Detecting what kind of input the user gave
# ---------------------------------------------------------------------------

_JIRA_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")          # PROJ-123
_AZURE_RE = re.compile(r"^(?:AB#)?(\d+)$")               # AB#456 or 456

def classify_input(text: str) -> str:
    """Return 'jira', 'azure', or 'text' based on the input shape."""
    t = text.strip()
    if _JIRA_RE.match(t):
        return "jira"
    if _AZURE_RE.match(t):
        return "azure"
    return "text"


# ---------------------------------------------------------------------------
# Acceptance-criteria extraction (shared)
# ---------------------------------------------------------------------------

def _split_criteria(text: str) -> List[str]:
    """Pull bullet-like acceptance criteria out of a description blob."""
    if not text:
        return []
    lines = re.split(r"[\n\r]+", text)
    out = []
    for ln in lines:
        s = ln.strip(" \t-*•·>")
        # keep lines that look like criteria (Given/When/Then, "should", "must")
        if s and (re.match(r"(?i)(given|when|then|and|should|must|verify|ensure)\b", s)
                  or ln.strip().startswith(("-", "*", "•"))):
            out.append(s)
    return out[:30]


def _extract_constraints(text: str) -> List[str]:
    """Pull out security / validation constraints mentioned in the story."""
    if not text:
        return []
    keywords = ("password", "auth", "token", "encrypt", "valid", "invalid",
                "permission", "role", "rate limit", "sql", "xss", "sanitize",
                "required", "mandatory", "must not", "reject", "lockout")
    out = []
    for ln in re.split(r"[\n\r.]+", text):
        s = ln.strip()
        if s and any(k in s.lower() for k in keywords):
            out.append(s)
    return out[:20]


# ---------------------------------------------------------------------------
# Jira
# ---------------------------------------------------------------------------

def fetch_jira(ticket_id: str) -> Requirement:
    base = os.environ.get("JIRA_BASE")
    email = os.environ.get("JIRA_EMAIL")
    pat = os.environ.get("JIRA_PAT")
    if not (base and email and pat):
        raise RuntimeError(
            "Jira not configured. Set JIRA_BASE, JIRA_EMAIL, JIRA_PAT "
            "as environment variables (never hardcode them).")

    url = f"{base.rstrip('/')}/rest/api/3/issue/{ticket_id}"
    token = base64.b64encode(f"{email}:{pat}".encode()).decode()
    req = urllib.request.Request(url, headers={
        "Authorization": f"Basic {token}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Jira API error {e.code}: check ticket ID and token.")

    fields = data.get("fields", {})
    title = fields.get("summary", ticket_id)
    # Jira cloud descriptions are Atlassian Document Format (ADF) -> flatten text
    story = _adf_to_text(fields.get("description")) or ""
    return Requirement(
        source=f"jira:{ticket_id}",
        title=title,
        story=story,
        acceptance_criteria=_split_criteria(story),
        constraints=_extract_constraints(story + " " + title),
        raw_url=f"{base.rstrip('/')}/browse/{ticket_id}",
    )


def _adf_to_text(node) -> str:
    """Flatten Atlassian Document Format JSON into plain text."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    text = ""
    if isinstance(node, dict):
        if node.get("type") == "text":
            text += node.get("text", "")
        for child in node.get("content", []) or []:
            text += _adf_to_text(child)
        if node.get("type") in ("paragraph", "listItem", "heading"):
            text += "\n"
    elif isinstance(node, list):
        for child in node:
            text += _adf_to_text(child)
    return text


# ---------------------------------------------------------------------------
# Azure DevOps
# ---------------------------------------------------------------------------

def fetch_azure(work_item_id: str) -> Requirement:
    org = os.environ.get("AZURE_ORG")
    project = os.environ.get("AZURE_PROJECT")
    pat = os.environ.get("AZURE_PAT")
    if not (org and project and pat):
        raise RuntimeError(
            "Azure DevOps not configured. Set AZURE_ORG, AZURE_PROJECT, "
            "AZURE_PAT as environment variables (never hardcode them).")

    wid = work_item_id.replace("AB#", "").strip()
    url = (f"https://dev.azure.com/{org}/{project}/_apis/wit/workitems/"
           f"{wid}?$expand=all&api-version=7.0")
    # Azure DevOps uses Basic auth with an empty username and the PAT as password
    token = base64.b64encode(f":{pat}".encode()).decode()
    req = urllib.request.Request(url, headers={
        "Authorization": f"Basic {token}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Azure API error {e.code}: check work item ID and token.")

    f = data.get("fields", {})
    title = f.get("System.Title", f"Work item {wid}")
    story = _strip_html(f.get("System.Description", "")) or ""
    criteria_raw = _strip_html(f.get("Microsoft.VSTS.Common.AcceptanceCriteria", ""))
    criteria = _split_criteria(criteria_raw) or _split_criteria(story)
    return Requirement(
        source=f"azure:{wid}",
        title=title,
        story=story,
        acceptance_criteria=criteria,
        constraints=_extract_constraints(story + " " + criteria_raw + " " + title),
        raw_url=f"https://dev.azure.com/{org}/{project}/_workitems/edit/{wid}",
    )


def _strip_html(html: str) -> str:
    """Crude HTML -> text (Azure descriptions are HTML)."""
    if not html:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", html)
    text = re.sub(r"</(p|div|li)>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ---------------------------------------------------------------------------
# Free text
# ---------------------------------------------------------------------------

def from_text(text: str, title: str = "Pasted requirement") -> Requirement:
    return Requirement(
        source="text",
        title=title,
        story=text,
        acceptance_criteria=_split_criteria(text),
        constraints=_extract_constraints(text),
    )


# ---------------------------------------------------------------------------
# Single entry point
# ---------------------------------------------------------------------------

def fetch_requirement(user_input: str) -> Requirement:
    """
    Turn whatever the user typed into a normalized Requirement.
    Routes to Jira / Azure / text automatically.
    """
    kind = classify_input(user_input)
    if kind == "jira":
        return fetch_jira(user_input.strip())
    if kind == "azure":
        return fetch_azure(user_input.strip())
    return from_text(user_input)
