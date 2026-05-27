"""
OAuth 2.0 flows for Jira (Atlassian) and Azure DevOps (via Microsoft Entra ID).

This handles the THREE-legged OAuth dance:
  1. build_authorize_url()  -> where we send the client's browser to log in
  2. (client logs in & approves on the provider's site)
  3. exchange_code()        -> turn the returned ?code into tokens
  4. refresh()              -> get a fresh access token when it expires

All client IDs/secrets come from environment variables, never hardcoded.

IMPORTANT — Azure uses Microsoft Entra ID, NOT the old Azure DevOps OAuth
(Microsoft is retiring that). The scope for Azure DevOps access through Entra
is the fixed resource GUID below + /.default.

REQUIRED ENVIRONMENT VARIABLES
  Jira (Atlassian):
    JIRA_OAUTH_CLIENT_ID
    JIRA_OAUTH_CLIENT_SECRET
    JIRA_OAUTH_REDIRECT_URI    e.g. https://taas.yourdomain.com/oauth/jira/callback
  Azure (Entra):
    AZURE_OAUTH_CLIENT_ID
    AZURE_OAUTH_CLIENT_SECRET
    AZURE_OAUTH_TENANT         your Entra tenant id (or 'organizations')
    AZURE_OAUTH_REDIRECT_URI   e.g. https://taas.yourdomain.com/oauth/azure/callback
"""
from __future__ import annotations

import os
import time
import json
import secrets
import urllib.parse
import urllib.request
from typing import Tuple

from taas.auth.token_store import StoredToken

# Azure DevOps's fixed resource ID in Entra. Combined with /.default this asks
# for Azure DevOps access via Microsoft Entra ID.
AZURE_DEVOPS_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"


# ---------------------------------------------------------------------------
# Provider configuration
# ---------------------------------------------------------------------------

def _jira_cfg():
    cid = os.environ.get("JIRA_OAUTH_CLIENT_ID")
    sec = os.environ.get("JIRA_OAUTH_CLIENT_SECRET")
    redirect = os.environ.get("JIRA_OAUTH_REDIRECT_URI")
    if not (cid and sec and redirect):
        raise RuntimeError("Jira OAuth not configured (set JIRA_OAUTH_CLIENT_ID, "
                           "JIRA_OAUTH_CLIENT_SECRET, JIRA_OAUTH_REDIRECT_URI).")
    return cid, sec, redirect


def _azure_cfg():
    cid = os.environ.get("AZURE_OAUTH_CLIENT_ID")
    sec = os.environ.get("AZURE_OAUTH_CLIENT_SECRET")
    tenant = os.environ.get("AZURE_OAUTH_TENANT", "organizations")
    redirect = os.environ.get("AZURE_OAUTH_REDIRECT_URI")
    if not (cid and sec and redirect):
        raise RuntimeError("Azure OAuth not configured (set AZURE_OAUTH_CLIENT_ID, "
                           "AZURE_OAUTH_CLIENT_SECRET, AZURE_OAUTH_REDIRECT_URI).")
    return cid, sec, tenant, redirect


# ---------------------------------------------------------------------------
# Step 1: where to send the client's browser
# ---------------------------------------------------------------------------

def build_authorize_url(provider: str, state: str) -> str:
    """
    Returns the provider login URL. `state` is a random anti-CSRF token we
    generate, store against the session, and verify on the callback.
    """
    if provider == "jira":
        cid, _, redirect = _jira_cfg()
        params = {
            "audience": "api.atlassian.com",
            "client_id": cid,
            "scope": "read:jira-work read:jira-user offline_access",
            "redirect_uri": redirect,
            "state": state,
            "response_type": "code",
            "prompt": "consent",
        }
        return "https://auth.atlassian.com/authorize?" + urllib.parse.urlencode(params)

    if provider == "azure":
        cid, _, tenant, redirect = _azure_cfg()
        params = {
            "client_id": cid,
            "response_type": "code",
            "redirect_uri": redirect,
            "response_mode": "query",
            "scope": f"{AZURE_DEVOPS_RESOURCE}/.default offline_access",
            "state": state,
        }
        return (f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize?"
                + urllib.parse.urlencode(params))

    raise ValueError(f"Unknown provider: {provider}")


def new_state() -> str:
    """Anti-CSRF state token for the OAuth round trip."""
    return secrets.token_urlsafe(24)


# ---------------------------------------------------------------------------
# Step 3: exchange the returned code for tokens
# ---------------------------------------------------------------------------

def _post_form(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def exchange_code(provider: str, code: str, tenant_id: str) -> StoredToken:
    if provider == "jira":
        cid, sec, redirect = _jira_cfg()
        tok = _post_form("https://auth.atlassian.com/oauth/token", {
            "grant_type": "authorization_code",
            "client_id": cid,
            "client_secret": sec,
            "code": code,
            "redirect_uri": redirect,
        })
        meta = {}
        # Jira needs the cloud id to call the API; fetch it once.
        try:
            meta["cloud_id"] = _jira_cloud_id(tok["access_token"])
        except Exception:
            pass
        return StoredToken(
            tenant_id=tenant_id, provider="jira",
            access_token=tok["access_token"],
            refresh_token=tok.get("refresh_token"),
            expires_at=time.time() + tok.get("expires_in", 3600),
            meta=meta,
        )

    if provider == "azure":
        cid, sec, tenant, redirect = _azure_cfg()
        tok = _post_form(
            f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token", {
                "grant_type": "authorization_code",
                "client_id": cid,
                "client_secret": sec,
                "code": code,
                "redirect_uri": redirect,
                "scope": f"{AZURE_DEVOPS_RESOURCE}/.default offline_access",
            })
        return StoredToken(
            tenant_id=tenant_id, provider="azure",
            access_token=tok["access_token"],
            refresh_token=tok.get("refresh_token"),
            expires_at=time.time() + tok.get("expires_in", 3600),
            meta={},
        )

    raise ValueError(f"Unknown provider: {provider}")


# ---------------------------------------------------------------------------
# Step 4: refresh an expired access token
# ---------------------------------------------------------------------------

def refresh(token: StoredToken) -> StoredToken:
    if not token.refresh_token:
        raise RuntimeError("No refresh token available; client must reconnect.")

    if token.provider == "jira":
        cid, sec, _ = _jira_cfg()
        tok = _post_form("https://auth.atlassian.com/oauth/token", {
            "grant_type": "refresh_token",
            "client_id": cid,
            "client_secret": sec,
            "refresh_token": token.refresh_token,
        })
    elif token.provider == "azure":
        cid, sec, tenant, _ = _azure_cfg()
        tok = _post_form(
            f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token", {
                "grant_type": "refresh_token",
                "client_id": cid,
                "client_secret": sec,
                "refresh_token": token.refresh_token,
                "scope": f"{AZURE_DEVOPS_RESOURCE}/.default offline_access",
            })
    else:
        raise ValueError(f"Unknown provider: {token.provider}")

    token.access_token = tok["access_token"]
    token.refresh_token = tok.get("refresh_token", token.refresh_token)
    token.expires_at = time.time() + tok.get("expires_in", 3600)
    return token


def _jira_cloud_id(access_token: str) -> str:
    """Jira API calls need the cloud id of the site the token can access."""
    req = urllib.request.Request(
        "https://api.atlassian.com/oauth/token/accessible-resources",
        headers={"Authorization": f"Bearer {access_token}",
                 "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        resources = json.load(r)
    if resources:
        return resources[0]["id"]
    raise RuntimeError("No accessible Jira sites for this token.")
