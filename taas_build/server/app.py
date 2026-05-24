"""
TaaS API server — the first real piece of the backend.

Turns the engine into something a browser / the future React Flow UI can
talk to. Wraps the existing modules behind REST endpoints, enforces the
validation gate as proper HTTP errors, and serves a self-contained HTML
page at "/" so opening localhost:8080 shows a live, clickable interface.

Run:
    python -m uvicorn server.app:app --port 8080 --reload
or:
    python server/app.py

Endpoints:
    GET  /                 built-in browser UI (no React build needed)
    GET  /health           liveness probe
    POST /ingest/story     story (title + ACs)  -> IR cases
    POST /ingest/csv       CSV text             -> IR cases
    POST /analyze/code     component descriptor -> edge/negative/security IR
    POST /validate         raw IR cases         -> validation report
    POST /generate         IR suite             -> pytest+Selenium (gated)
    POST /pipeline/demo    runs the full canned pipeline end-to-end
"""
from __future__ import annotations

import os
import sys
from typing import List, Optional, Dict, Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel

from taas.ir.schema import TestSuite, TestCase
from taas.ir.validator import IRValidator
from taas.ai.generator import StubIRGenerator   # swap -> OllamaIRGenerator
from taas.ingest.adapters import JiraConnector, FileIngestor, CodebaseAnalyzer
from taas.translate.engine import SeleniumTranslator, IRValidationError
from taas.execute.runner import SimulationRunner  # swap -> SeleniumRunner
from taas.ingest.upload_parser import UploadParser
from taas.ai.ollama_generator import OllamaAIGenerator

app = FastAPI(title="TaaS IR Engine", version="0.4.0")

# One generator instance. Swapping to the real LLM is the only change needed:
#   from taas.ai.generator import OllamaIRGenerator
#   ai = OllamaIRGenerator(model="qwen2.5-coder:14b")
ai = StubIRGenerator()

# The runner. Swap to SeleniumRunner once Chrome + a live target exist:
#   from taas.execute.runner import SeleniumRunner
#   runner = SeleniumRunner(remote_url="http://selenoid:4444/wd/hub")
runner = SimulationRunner()

# Ollama AI generator — connects to local Ollama instance.
# Change model to "llama3.2:3b" for slower machines, "qwen2.5-coder:14b" for fast ones.
ollama_ai = OllamaAIGenerator(model="qwen2.5-coder:7b")

# In-memory run history (Postgres in production).
RUN_HISTORY: List[Dict[str, Any]] = []


def _build_demo_suite() -> TestSuite:
    """The canned 5-test suite used by demo + run endpoints."""
    suite = TestSuite(suite_name="Password Reset & Auth",
                      base_url="https://demo-app.local")
    story = JiraConnector().fetch_story({
        "key": "PROJ-123",
        "fields": {
            "summary": "User can reset password via email link",
            "description": ("- User can enter their email address\n"
                            "- User can click the reset button\n"
                            "- User should see a confirmation message: Reset link sent\n"),
        },
    })
    suite.cases += ai.story_to_ir(story.title, story.acceptance_criteria, story.source)
    csv_text = ("key,title,acceptance_criteria\n"
                "CSV-1,Login with valid creds,"
                '"User can enter email; User can enter password; '
                'User can click submit; User should see Dashboard"\n')
    for s in FileIngestor().parse_csv(csv_text):
        suite.cases += ai.story_to_ir(s.title, s.acceptance_criteria, s.source)
    for comp in CodebaseAnalyzer().analyze([
        {"name": "LoginForm", "fields": ["email", "password"],
         "endpoint": "/api/auth/login"}]):
        suite.cases += ai.code_to_ir(comp.name, comp.fields, comp.endpoint, comp.source)
    return suite


# ---- request models ---------------------------------------------------
class StoryReq(BaseModel):
    title: str
    acceptance_criteria: List[str]
    source: str = "story:manual"


class CsvReq(BaseModel):
    csv_text: str


class CodeReq(BaseModel):
    name: str
    fields: List[str]
    endpoint: str = "/"


class GenerateReq(BaseModel):
    suite_name: str = "Generated Suite"
    base_url: str = "http://localhost:3000"
    cases: List[Dict[str, Any]]


# ---- helpers ----------------------------------------------------------
def _cases_to_dicts(cases: List[TestCase]) -> List[dict]:
    return [c.model_dump(mode="json") for c in cases]


def _validation_report(result) -> dict:
    return {
        "ok": result.ok,
        "errors": [vars(i) | {"severity": i.severity.value} for i in result.errors],
        "warnings": [vars(i) | {"severity": i.severity.value} for i in result.warnings],
    }


# ---- real site suite: the-internet.herokuapp.com/login ---------------
TARGET = "https://the-internet.herokuapp.com"

def _build_live_suite() -> TestSuite:
    """
    A test suite hand-crafted for the-internet.herokuapp.com/login.
    Every locator, credential and expected outcome matches the REAL site HTML.
    Tests:
      1. Happy path  — valid login -> lands on /secure
      2. Wrong password — stays on /login, shows error flash
      3. Empty username — validation error shown
      4. Empty password — validation error shown
      5. Logout         — secure page -> logout -> back at /login
      6. Security probe — injection attempt stays on /login (no bypass)
    """
    from taas.ir.schema import (
        TestSuite, TestCase, TestStep, TestCategory, ActionType, Locator
    )
    def step(action, description, locator_strategy=None, locator_value=None, value=None, timeout=10):
        loc = Locator(strategy=locator_strategy, value=locator_value) if locator_strategy else None
        return TestStep(action=action, locator=loc, value=value,
                        description=description, timeout=timeout)

    suite = TestSuite(suite_name="The Internet — Login Page", base_url=TARGET)

    # 1. Happy path: valid credentials
    suite.cases.append(TestCase(
        name="Login with valid credentials",
        category=TestCategory.HAPPY_PATH,
        source="live:the-internet/login",
        tags=["login", "happy_path"],
        steps=[
            step(ActionType.NAVIGATE, "Open login page", value="/login"),
            step(ActionType.FILL, "Enter valid username",
                 "id", "username", "tomsmith"),
            step(ActionType.FILL, "Enter valid password",
                 "id", "password", "SuperSecretPassword!"),
            step(ActionType.CLICK, "Click Login button",
                 "css", "button[type='submit']"),
            step(ActionType.ASSERT_URL, "Should land on /secure", value="/secure"),
            step(ActionType.ASSERT_TEXT, "Success flash message shown",
                 "css", ".flash.success", "You logged into a secure area!"),
            step(ActionType.SCREENSHOT, "Capture logged-in state"),
        ],
    ))

    # 2. Wrong password
    suite.cases.append(TestCase(
        name="Login with wrong password",
        category=TestCategory.NEGATIVE,
        source="live:the-internet/login",
        tags=["login", "negative"],
        steps=[
            step(ActionType.NAVIGATE, "Open login page", value="/login"),
            step(ActionType.FILL, "Enter valid username",
                 "id", "username", "tomsmith"),
            step(ActionType.FILL, "Enter WRONG password",
                 "id", "password", "wrongpassword"),
            step(ActionType.CLICK, "Click Login button",
                 "css", "button[type='submit']"),
            step(ActionType.ASSERT_URL, "Should stay on /login", value="/login"),
            step(ActionType.ASSERT_TEXT, "Error message shown",
                 "css", ".flash.error", "Your password is invalid!"),
        ],
    ))

    # 3. Empty username
    suite.cases.append(TestCase(
        name="Login with empty username",
        category=TestCategory.EDGE_CASE,
        source="live:the-internet/login",
        tags=["login", "edge"],
        steps=[
            step(ActionType.NAVIGATE, "Open login page", value="/login"),
            step(ActionType.FILL, "Leave username empty",
                 "id", "username", ""),
            step(ActionType.FILL, "Enter password",
                 "id", "password", "SuperSecretPassword!"),
            step(ActionType.CLICK, "Click Login button",
                 "css", "button[type='submit']"),
            step(ActionType.ASSERT_TEXT, "Error: username required",
                 "css", ".flash.error", "Your username is invalid!"),
        ],
    ))

    # 4. Empty password
    suite.cases.append(TestCase(
        name="Login with empty password",
        category=TestCategory.EDGE_CASE,
        source="live:the-internet/login",
        tags=["login", "edge"],
        steps=[
            step(ActionType.NAVIGATE, "Open login page", value="/login"),
            step(ActionType.FILL, "Enter username",
                 "id", "username", "tomsmith"),
            step(ActionType.FILL, "Leave password empty",
                 "id", "password", ""),
            step(ActionType.CLICK, "Click Login button",
                 "css", "button[type='submit']"),
            step(ActionType.ASSERT_TEXT, "Error: password required",
                 "css", ".flash.error", "Your password is invalid!"),
        ],
    ))

    # 5. Logout flow
    suite.cases.append(TestCase(
        name="Login then logout",
        category=TestCategory.HAPPY_PATH,
        source="live:the-internet/login",
        tags=["login", "logout", "happy_path"],
        steps=[
            step(ActionType.NAVIGATE, "Open login page", value="/login"),
            step(ActionType.FILL, "Enter username",
                 "id", "username", "tomsmith"),
            step(ActionType.FILL, "Enter password",
                 "id", "password", "SuperSecretPassword!"),
            step(ActionType.CLICK, "Submit login",
                 "css", "button[type='submit']"),
            step(ActionType.ASSERT_URL, "Landed on secure page", value="/secure"),
            step(ActionType.CLICK, "Click Logout",
                 "css", "a.button", "Logout"),
            step(ActionType.ASSERT_URL, "Back at login page", value="/login"),
        ],
    ))

    # 6. Security: SQL injection probe
    suite.cases.append(TestCase(
        name="SQL injection probe on login",
        category=TestCategory.SECURITY,
        source="live:the-internet/login",
        tags=["security", "injection"],
        steps=[
            step(ActionType.NAVIGATE, "Open login page", value="/login"),
            step(ActionType.FILL, "Inject into username",
                 "id", "username", "' OR '1'='1"),
            step(ActionType.FILL, "Inject into password",
                 "id", "password", "' OR '1'='1"),
            step(ActionType.CLICK, "Submit injection attempt",
                 "css", "button[type='submit']"),
            step(ActionType.ASSERT_URL,
                 "Must NOT reach /secure (injection blocked)", value="/login"),
            step(ActionType.SECURITY_SCAN,
                 "Run ZAP DAST scan on login endpoint", value="/login"),
        ],
    ))

    return suite


# ---- endpoints --------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok", "generator": type(ai).__name__}


@app.post("/ingest/story")
def ingest_story(req: StoryReq):
    cases = ai.story_to_ir(req.title, req.acceptance_criteria, req.source)
    return {"cases": _cases_to_dicts(cases)}


@app.post("/ingest/csv")
def ingest_csv(req: CsvReq):
    stories = FileIngestor().parse_csv(req.csv_text)
    out: List[TestCase] = []
    for s in stories:
        out += ai.story_to_ir(s.title, s.acceptance_criteria, s.source)
    return {"stories_parsed": len(stories), "cases": _cases_to_dicts(out)}


@app.post("/analyze/code")
def analyze_code(req: CodeReq):
    cases = ai.code_to_ir(req.name, req.fields, req.endpoint, f"code:{req.name}")
    return {"cases": _cases_to_dicts(cases)}


@app.post("/validate")
def validate(req: GenerateReq):
    result = IRValidator().validate_suite(req.cases)
    return _validation_report(result)


@app.post("/generate")
def generate(req: GenerateReq):
    # Build the suite, then let the translator's gate enforce validity.
    try:
        suite = TestSuite(
            suite_name=req.suite_name, base_url=req.base_url,
            cases=[TestCase(**c) for c in req.cases],
        )
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Malformed IR: {e}")
    try:
        code = SeleniumTranslator().render_suite(suite)
    except IRValidationError as e:
        # Validation gate fired -> 422 with structured issues for the UI.
        raise HTTPException(status_code=422, detail={
            "message": "IR failed validation; codegen refused.",
            "issues": [vars(i) | {"severity": i.severity.value}
                       for i in e.result.errors],
        })
    return {
        "suite_name": suite.suite_name,
        "coverage": suite.coverage_summary(),
        "code": code,
    }


@app.post("/pipeline/demo")
def pipeline_demo():
    """The canned end-to-end run, returned as JSON for the browser UI."""
    suite = TestSuite(suite_name="Password Reset & Auth",
                      base_url="https://demo-app.local")
    # 1. Jira
    story = JiraConnector().fetch_story({
        "key": "PROJ-123",
        "fields": {
            "summary": "User can reset password via email link",
            "description": ("- User can enter their email address\n"
                            "- User can click the reset button\n"
                            "- User should see a confirmation message: Reset link sent\n"),
        },
    })
    suite.cases += ai.story_to_ir(story.title, story.acceptance_criteria, story.source)
    # 2. CSV
    csv_text = ("key,title,acceptance_criteria\n"
                "CSV-1,Login with valid creds,"
                '"User can enter email; User can enter password; '
                'User can click submit; User should see Dashboard"\n')
    for s in FileIngestor().parse_csv(csv_text):
        suite.cases += ai.story_to_ir(s.title, s.acceptance_criteria, s.source)
    # 3. Code
    for comp in CodebaseAnalyzer().analyze([
        {"name": "LoginForm", "fields": ["email", "password"],
         "endpoint": "/api/auth/login"}]):
        suite.cases += ai.code_to_ir(comp.name, comp.fields, comp.endpoint, comp.source)
    # 4+5. validate + generate
    code = SeleniumTranslator().render_suite(suite)
    return {
        "coverage": suite.coverage_summary(),
        "ir": suite.model_dump(mode="json"),
        "code": code,
    }


@app.post("/run/demo")
def run_demo():
    """Build the canned suite, execute it, store the result, return it."""
    suite = _build_demo_suite()
    # Codegen gate still applies — never run IR we wouldn't generate.
    SeleniumTranslator().render_suite(suite)
    result = runner.run_suite(suite).to_dict()
    RUN_HISTORY.insert(0, {
        "id": len(RUN_HISTORY) + 1,
        "suite_name": result["suite_name"],
        "started_at": result["started_at"],
        "summary": result["summary"],
    })
    return result


import os as _os
_TEMPLATE_DIR = _os.path.join(_os.path.dirname(__file__), "templates")

@app.get("/templates/csv")
def download_csv_template():
    """Serve the blank CSV template for clients to fill in."""
    path = _os.path.join(_TEMPLATE_DIR, "taas_test_template.csv")
    return FileResponse(path, media_type="text/csv", filename="taas_test_template.csv")

@app.get("/templates/excel")
def download_excel_template():
    """Serve the Excel template (with dropdowns + instructions) for clients."""
    path = _os.path.join(_TEMPLATE_DIR, "taas_test_template.xlsx")
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="taas_test_template.xlsx",
    )


@app.post("/upload/run")
async def upload_and_run(file: UploadFile = File(...)):
    """Accept an uploaded Excel or CSV file, parse it, run it immediately."""
    data = await file.read()
    fname = (file.filename or "").lower()
    parser = UploadParser()
    try:
        if fname.endswith(".xlsx") or fname.endswith(".xls"):
            cases = parser.parse_excel_bytes(data, source=f"upload:{file.filename}")
        else:
            cases = parser.parse_csv_bytes(data, source=f"upload:{file.filename}")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not parse file: {e}")
    if not cases:
        raise HTTPException(status_code=400, detail="No test cases found in file.")
    suite = TestSuite(suite_name=f"Uploaded: {file.filename}", base_url="", cases=cases)
    val = IRValidator().validate_suite([c.model_dump(mode="json") for c in cases])
    if not val.ok:
        raise HTTPException(status_code=422, detail={
            "message": "File has invalid test steps.",
            "issues": [{"path": i.path, "message": i.message} for i in val.errors],
        })
    result = runner.run_suite(suite).to_dict()
    RUN_HISTORY.insert(0, {"id": len(RUN_HISTORY)+1, "suite_name": result["suite_name"],
                           "started_at": result["started_at"], "summary": result["summary"]})
    return result


class AiGenerateReq(BaseModel):
    url: str


@app.post("/ai/generate-and-run")
def ai_generate_and_run(req: AiGenerateReq):
    """Use Ollama to analyse a URL, generate test cases, and run them."""
    if not ollama_ai.is_available():
        raise HTTPException(status_code=503, detail={
            "message": "Ollama is not running or model not pulled.",
            "fix": [
                "1. Download Ollama: https://ollama.com/download",
                f"2. Run in terminal: ollama pull {ollama_ai.model}",
                "3. Try again — Ollama starts automatically.",
            ],
            "available_models": ollama_ai.list_models(),
        })
    try:
        cases = ollama_ai.generate_for_url(req.url)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    base = "/".join(req.url.split("/")[:3])
    suite = TestSuite(suite_name=f"AI: {req.url}", base_url=base, cases=cases)
    result = runner.run_suite(suite, target_url=req.url).to_dict()
    RUN_HISTORY.insert(0, {"id": len(RUN_HISTORY)+1, "suite_name": result["suite_name"],
                           "started_at": result["started_at"], "summary": result["summary"]})
    return result


@app.get("/ai/status")
def ai_status():
    available = ollama_ai.is_available()
    return {"ollama_running": available, "configured_model": ollama_ai.model,
            "model_ready": available, "available_models": ollama_ai.list_models(),
            "install_url": "https://ollama.com/download"}

@app.post("/run/live")
def run_live():
    """Run the real test suite against the-internet.herokuapp.com."""
    suite = _build_live_suite()
    SeleniumTranslator().render_suite(suite)
    result = runner.run_suite(suite, target_url=TARGET).to_dict()
    RUN_HISTORY.insert(0, {
        "id": len(RUN_HISTORY) + 1,
        "suite_name": result["suite_name"],
        "started_at": result["started_at"],
        "summary": result["summary"],
    })
    return result


@app.get("/runs")
def list_runs():
    return {"runs": RUN_HISTORY}


@app.get("/", response_class=HTMLResponse)
def index():
    return _INDEX_HTML


# ---- self-contained browser UI (no build step) -----------------------
_INDEX_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TaaS Dashboard</title>
<style>
:root{--bg:#0f1117;--panel:#171a23;--panel2:#1d2230;--border:#262b38;--text:#e6e9ef;
  --muted:#8b93a7;--pass:#5dcaa5;--fail:#e24b4a;--err:#ef9f27;--accent:#378add;
  --mono:'SFMono-Regular',Consolas,monospace;}
*{box-sizing:border-box;}
body{margin:0;background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,sans-serif;}
header{padding:16px 24px;border-bottom:1px solid var(--border);display:flex;
  align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;}
h1{font-size:17px;margin:0;font-weight:500;}
.sub{color:var(--muted);font-size:12px;margin-top:2px;}
main{padding:20px 24px;}
/* tabs */
.tabs{display:flex;gap:0;background:var(--panel2);border:1px solid var(--border);
  border-radius:10px;overflow:hidden;margin-bottom:20px;width:fit-content;}
.tab{background:transparent;color:var(--muted);border:0;padding:10px 20px;
  font-size:13px;cursor:pointer;transition:.12s;}
.tab.active{background:var(--accent);color:#fff;}
.tab:hover:not(.active){color:var(--text);}
/* panels */
.panel{display:none;} .panel.show{display:block;}
/* url bar */
.urlbar{display:flex;gap:10px;margin-bottom:16px;align-items:center;}
input[type=text]{background:var(--panel);border:1px solid var(--border);color:var(--text);
  border-radius:8px;padding:10px 14px;font-size:13px;flex:1;}
input[type=text]:focus{outline:none;border-color:var(--accent);}
/* buttons */
.btn{background:var(--accent);color:#fff;border:0;border-radius:8px;
  padding:10px 20px;font-size:13px;font-weight:500;cursor:pointer;white-space:nowrap;}
.btn:hover{filter:brightness(1.12);}
.btn:disabled{opacity:.45;cursor:wait;}
.btn.sec{background:var(--panel);border:1px solid var(--border);color:var(--text);}
.btn.sec:hover{border-color:var(--accent);color:var(--accent);}
/* upload drop zone */
.dropzone{border:2px dashed var(--border);border-radius:12px;padding:40px;
  text-align:center;cursor:pointer;transition:.12s;background:var(--panel);}
.dropzone:hover,.dropzone.over{border-color:var(--accent);}
.dropzone p{margin:8px 0;color:var(--muted);font-size:14px;}
.dropzone .big{font-size:32px;margin:0;}
input[type=file]{display:none;}
/* ollama status */
.status-row{display:flex;align-items:center;gap:10px;background:var(--panel);
  border:1px solid var(--border);border-radius:8px;padding:12px 16px;margin-bottom:16px;}
.dot{width:10px;height:10px;border-radius:50%;background:var(--err);flex-shrink:0;}
.dot.ok{background:var(--pass);}
.status-msg{font-size:13px;color:var(--muted);}
/* results */
.cards{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-bottom:12px;}
.card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:12px 14px;}
.card .n{font-size:24px;font-weight:500;}
.card .l{color:var(--muted);font-size:11px;margin-top:2px;}
.n.pass{color:var(--pass);}.n.fail{color:var(--fail);}.n.err{color:var(--err);}
.bar{height:7px;border-radius:5px;background:var(--panel2);overflow:hidden;
  display:flex;margin:0 0 20px;}
.bar>i{display:block;height:100%;}
table{width:100%;border-collapse:collapse;font-size:13px;}
th{text-align:left;color:var(--muted);font-weight:500;font-size:11px;
  text-transform:uppercase;letter-spacing:.04em;padding:8px 10px;
  border-bottom:1px solid var(--border);}
td{padding:10px;border-bottom:1px solid var(--border);vertical-align:top;}
tr:last-child td{border-bottom:0;}
tr:hover td{background:var(--panel);}
.pill{font-size:11px;padding:3px 9px;border-radius:20px;font-weight:500;}
.pill.passed{background:rgba(93,202,165,.15);color:var(--pass);}
.pill.failed{background:rgba(226,75,74,.15);color:var(--fail);}
.pill.error{background:rgba(239,159,39,.15);color:var(--err);}
.cat{font-size:11px;color:var(--muted);background:var(--panel2);padding:2px 8px;border-radius:5px;}
.reason{color:var(--fail);font-size:12px;margin-top:3px;}
.reason.ok{color:var(--muted);}
.src{font-family:var(--mono);font-size:11px;color:var(--muted);margin-top:2px;}
.steps-btn{font-size:11px;color:var(--accent);cursor:pointer;background:none;
  border:none;padding:0;margin-top:4px;}
.steps-detail{display:none;margin-top:6px;border-left:2px solid var(--border);padding-left:10px;}
.steps-detail.open{display:block;}
.srow{font-size:11.5px;color:var(--muted);padding:2px 0;}
.srow.passed{color:var(--pass);}.srow.failed,.srow.error{color:var(--fail);}
.empty{color:var(--muted);font-size:14px;padding:40px 0;text-align:center;}
.meta{color:var(--muted);font-size:12px;margin-top:12px;}
.error-box{background:rgba(226,75,74,.1);border:1px solid rgba(226,75,74,.3);
  border-radius:8px;padding:14px 16px;font-size:13px;color:var(--fail);}
.error-box ul{margin:8px 0 0 0;padding-left:18px;}
.error-box li{margin:4px 0;color:var(--text);}
.tmpl{background:var(--panel2);border:1px solid var(--border);border-radius:8px;
  padding:12px 16px;font-family:var(--mono);font-size:11.5px;color:var(--muted);
  white-space:pre;overflow-x:auto;margin-bottom:12px;}
</style></head>
<body>
<header>
  <div>
    <h1>TaaS — Test Results Dashboard</h1>
    <div class="sub" id="genlabel">loading…</div>
  </div>
</header>
<main>
<div class="tabs">
  <button class="tab active" onclick="showTab('demo')">&#9654; Demo</button>
  <button class="tab" onclick="showTab('live')">Live site</button>
  <button class="tab" onclick="showTab('upload')">&#8679; Upload file</button>
  <button class="tab" onclick="showTab('ai')">&#10024; AI Generate</button>
  <button class="tab" onclick="showTab('recording')">&#128249; Recording</button>
  <button class="tab" onclick="showTab('bugs')">&#128027; Bug Reports</button>
</div>

<!-- DEMO TAB -->
<div class="panel show" id="tab-demo">
  <p style="color:var(--muted);font-size:13px;margin:0 0 14px">Runs the built-in 5-test demo suite to show how the dashboard works.</p>
  <button class="btn" id="go-demo" onclick="runDemo()">&#9654; Run demo suite</button>
</div>

<!-- LIVE TAB -->
<div class="panel" id="tab-live">
  <p style="color:var(--muted);font-size:13px;margin:0 0 14px">Runs 6 pre-written tests against <b>the-internet.herokuapp.com</b> — a real public test website.</p>
  <button class="btn" id="go-live" onclick="runLive()">&#9654; Run live suite</button>
</div>

<!-- UPLOAD TAB -->
<div class="panel" id="tab-upload">
  <p style="color:var(--muted);font-size:13px;margin:0 0 8px">Upload your own test cases as an Excel (.xlsx) or CSV file and run them instantly.</p>
  <p style="color:var(--text);font-size:13px;margin:0 0 8px"><b>New to this?</b> Download a ready-made template, fill it in, and upload it back. The Excel version has dropdown menus so you can't enter invalid values.</p>
  <div style="display:flex;gap:10px;margin:0 0 16px;flex-wrap:wrap">
    <a class="btn" href="/templates/excel" download style="text-decoration:none">&#8681; Download Excel template</a>
    <a class="btn sec" href="/templates/csv" download style="text-decoration:none">&#8681; Download CSV template</a>
  </div>
  <p style="color:var(--muted);font-size:12px;margin:0 0 6px">Column format:</p>
  <div class="tmpl">test_name,category,url,action,locator_type,locator_value,input_value,expected
Valid login,happy_path,/login,fill,id,username,tomsmith,
Valid login,happy_path,,fill,id,password,SuperSecretPassword!,
Valid login,happy_path,,click,css,button[type=submit],,
Valid login,happy_path,,assert_text,css,.flash,,You logged into</div>
  <div class="dropzone" id="dropzone" onclick="document.getElementById('fileinput').click()"
       ondragover="event.preventDefault();this.classList.add('over')"
       ondragleave="this.classList.remove('over')"
       ondrop="handleDrop(event)">
    <p class="big">&#128196;</p>
    <p><b>Click to choose</b> or drag &amp; drop your file here</p>
    <p>Excel (.xlsx) or CSV</p>
  </div>
  <input type="file" id="fileinput" accept=".xlsx,.csv" onchange="handleFile(this.files[0])">
</div>

<!-- AI TAB -->
<div class="panel" id="tab-ai">
  <div class="status-row" id="ai-status-row">
    <div class="dot" id="ai-dot"></div>
    <div class="status-msg" id="ai-status-msg">Checking Ollama…</div>
    <a href="https://ollama.com/download" target="_blank"
       style="color:var(--accent);font-size:12px;margin-left:auto;">Get Ollama ↗</a>
  </div>
  <p style="color:var(--muted);font-size:13px;margin:0 0 12px">
    Paste any website URL. The AI will read the page structure and generate test cases automatically.</p>
  <div class="urlbar">
    <input type="text" id="ai-url" placeholder="https://the-internet.herokuapp.com/login" value="https://the-internet.herokuapp.com/login">
    <button class="btn" id="go-ai" onclick="runAI()">&#10024; Generate &amp; Run</button>
  </div>
  <div id="ai-setup" style="display:none">
    <div class="error-box">
      <b>Ollama is not running.</b> To use AI generation:
      <ul id="ai-fix-steps"></ul>
    </div>
  </div>
</div>

<!-- RECORDING TAB -->
<div class="panel" id="tab-recording">
  <p style="color:var(--muted);font-size:13px;margin:0 0 8px">Record the screen while a test runs, then stop it. Videos save to the <b>recordings</b> folder.</p>
  <p style="color:var(--text);font-size:13px;margin:0 0 14px">Video capture needs <b>FFmpeg</b> installed. Without it, everything still works — the video just won't be saved.</p>
  <div class="urlbar">
    <input type="text" id="rec-id" placeholder="Run ID (leave blank to auto-generate)">
    <button class="btn" onclick="recStart()">&#9654; Start Recording</button>
    <button class="btn sec" onclick="recStop()">&#9632; Stop Recording</button>
  </div>
  <div class="tmpl" id="rec-out" style="margin-top:14px">No recording yet.</div>
</div>

<!-- BUG REPORTS TAB -->
<div class="panel" id="tab-bugs">
  <p style="color:var(--muted);font-size:13px;margin:0 0 14px">View bug reports automatically created when tests fail. Enter a Run ID to load its reports.</p>
  <div class="urlbar">
    <input type="text" id="bug-id" placeholder="Run ID (e.g. abc123)">
    <button class="btn" onclick="bugLoad()">Get Bug Reports</button>
  </div>
  <div class="tmpl" id="bug-out" style="margin-top:14px">No reports loaded.</div>
</div>

<!-- RESULTS -->
<div id="results" style="margin-top:20px"></div>
</main>

<script>
// ---- tab switching ---------------------------------------------------
function showTab(name){
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('show'));
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.getElementById('tab-'+name).classList.add('show');
  event.target.classList.add('active');
}

// ---- recording + bug reports ----------------------------------------
let _recRunId = '';
function recStart(){
  let id = document.getElementById('rec-id').value || Math.random().toString(36).substr(2,8);
  _recRunId = id;
  document.getElementById('rec-id').value = id;
  fetch('/runs/'+id+'/recording/start',{method:'POST'})
    .then(r=>r.json())
    .then(d=>{document.getElementById('rec-out').textContent = JSON.stringify(d,null,2);});
}
function recStop(){
  if(!_recRunId){ document.getElementById('rec-out').textContent='Start a recording first.'; return; }
  fetch('/runs/'+_recRunId+'/recording/stop',{method:'POST'})
    .then(r=>r.json())
    .then(d=>{document.getElementById('rec-out').textContent = JSON.stringify(d,null,2);});
}
function bugLoad(){
  let id = document.getElementById('bug-id').value;
  if(!id){ document.getElementById('bug-out').textContent='Enter a Run ID.'; return; }
  fetch('/runs/'+id+'/bug-reports')
    .then(r=>r.json())
    .then(d=>{document.getElementById('bug-out').textContent = JSON.stringify(d,null,2);});
}

// ---- boot -----------------------------------------------------------
fetch('/health').then(r=>r.json()).then(d=>{
  document.getElementById('genlabel').textContent='Generator: '+d.generator+' · runner: simulation';
});
checkAiStatus();

function checkAiStatus(){
  fetch('/ai/status').then(r=>r.json()).then(d=>{
    const dot=document.getElementById('ai-dot');
    const msg=document.getElementById('ai-status-msg');
    if(d.ollama_running){
      dot.classList.add('ok');
      msg.textContent='Ollama running · model: '+d.configured_model+' · ready';
    } else {
      msg.textContent='Ollama not detected. Install it to use AI generation.';
    }
  }).catch(()=>{});
}

// ---- helpers --------------------------------------------------------
function esc(s){return (s||'').toString().replace(/&/g,'&amp;').replace(/</g,'&lt;');}
function ms(n){return n>=1000?(n/1000).toFixed(1)+'s':n+'ms';}
function setLoading(id,loading){
  const b=document.getElementById(id);
  if(!b)return;
  b.disabled=loading;
  b.textContent=loading?'Running…':b.dataset.label||b.textContent;
}

// ---- run functions --------------------------------------------------
async function runDemo(){
  document.getElementById('go-demo').dataset.label='▶ Run demo suite';
  setLoading('go-demo',true);
  await doRun('/run/demo',null);
  setLoading('go-demo',false);
}
async function runLive(){
  document.getElementById('go-live').dataset.label='▶ Run live suite';
  setLoading('go-live',true);
  await doRun('/run/live',null);
  setLoading('go-live',false);
}
async function runAI(){
  const url=document.getElementById('ai-url').value.trim();
  if(!url){alert('Please enter a URL.');return;}
  document.getElementById('go-ai').dataset.label='✨ Generate & Run';
  document.getElementById('go-ai').disabled=true;
  document.getElementById('go-ai').textContent='Generating… (this takes 30-90s)';
  document.getElementById('ai-setup').style.display='none';
  await doRun('/ai/generate-and-run',{url});
  document.getElementById('go-ai').disabled=false;
  document.getElementById('go-ai').textContent='✨ Generate & Run';
}

async function doRun(endpoint, body){
  document.getElementById('results').innerHTML='<p class="empty">Running tests…</p>';
  try{
    const opts={method:'POST'};
    if(body){opts.headers={'Content-Type':'application/json'};opts.body=JSON.stringify(body);}
    const r=await fetch(endpoint,opts);
    const d=await r.json();
    if(!r.ok){
      showError(d.detail);
      return;
    }
    renderResults(d);
  }catch(e){
    document.getElementById('results').innerHTML='<p class="empty" style="color:var(--fail)">Error: '+esc(e.message)+'</p>';
  }
}

function showError(detail){
  let html='<div class="error-box">';
  if(typeof detail==='string'){
    html+=esc(detail);
  } else {
    html+='<b>'+esc(detail.message)+'</b>';
    if(detail.fix){
      html+='<ul>';
      detail.fix.forEach(f=>{html+='<li>'+esc(f)+'</li>';});
      html+='</ul>';
      document.getElementById('ai-setup').style.display='block';
      const ul=document.getElementById('ai-fix-steps');
      ul.innerHTML='';
      detail.fix.forEach(f=>{const li=document.createElement('li');li.textContent=f;ul.appendChild(li);});
    }
    if(detail.issues){
      html+='<ul>';
      detail.issues.forEach(i=>{html+='<li>'+esc(i.path)+': '+esc(i.message)+'</li>';});
      html+='</ul>';
    }
  }
  html+='</div>';
  document.getElementById('results').innerHTML=html;
}

// ---- file upload ----------------------------------------------------
function handleDrop(e){
  e.preventDefault();
  document.getElementById('dropzone').classList.remove('over');
  const f=e.dataTransfer.files[0];
  if(f) handleFile(f);
}
async function handleFile(file){
  if(!file)return;
  document.getElementById('dropzone').innerHTML='<p class="big">&#9200;</p><p>Running <b>'+esc(file.name)+'</b>…</p>';
  document.getElementById('results').innerHTML='<p class="empty">Parsing and running…</p>';
  const fd=new FormData();
  fd.append('file',file);
  try{
    const r=await fetch('/upload/run',{method:'POST',body:fd});
    const d=await r.json();
    if(!r.ok){showError(d.detail);}
    else{renderResults(d);}
  }catch(e){
    document.getElementById('results').innerHTML='<p class="empty" style="color:var(--fail)">Error: '+esc(e.message)+'</p>';
  }
  document.getElementById('dropzone').innerHTML='<p class="big">&#128196;</p><p><b>Click to choose</b> or drag &amp; drop another file</p><p>Excel (.xlsx) or CSV</p>';
}

// ---- results renderer -----------------------------------------------
function renderResults(d){
  const s=d.summary;
  const bar=[['passed',s.passed,'var(--pass)'],['failed',s.failed,'var(--fail)'],['error',s.error,'var(--err)']];
  const barHtml=bar.map(([k,v,c])=>v?`<i style="width:${100*v/s.total}%;background:${c}"></i>`:'').join('');
  const rows=d.cases.map((c,i)=>{
    const stepsHtml=(c.steps||[]).map(st=>{
      const cls=st.status==='passed'?'passed':(st.status==='failed'||st.status==='error')?'error':'';
      return `<div class="srow ${cls}">${st.status==='passed'?'✓':'✗'} step ${st.index+1}: ${esc(st.action)}${st.description?' — '+esc(st.description):''}${st.detail?' → '+esc(st.detail):''}</div>`;
    }).join('');
    const reason=c.status==='passed'
      ?'<div class="reason ok">All steps passed</div>'
      :`<div class="reason">✗ ${esc(c.failure_reason)}${c.failed_at_step!=null?' (step '+(c.failed_at_step+1)+')':''}</div>`;
    return `<tr>
      <td><span class="pill ${c.status}">${c.status}</span></td>
      <td><div>${esc(c.name)}</div><div class="src">${esc(c.source)}</div>${reason}
          <button class="steps-btn" onclick="document.getElementById('s${i}').classList.toggle('open')">&#9656; steps (${(c.steps||[]).length})</button>
          <div class="steps-detail" id="s${i}">${stepsHtml}</div></td>
      <td><span class="cat">${esc((c.category||'').replace(/_/g,' '))}</span></td>
      <td style="white-space:nowrap">${ms(c.duration_ms)}</td>
    </tr>`;
  }).join('');
  document.getElementById('results').innerHTML=`
    <div class="cards">
      <div class="card"><div class="n">${s.total}</div><div class="l">Total</div></div>
      <div class="card"><div class="n pass">${s.passed}</div><div class="l">Passed</div></div>
      <div class="card"><div class="n fail">${s.failed}</div><div class="l">Failed</div></div>
      <div class="card"><div class="n err">${s.error}</div><div class="l">Errored</div></div>
      <div class="card"><div class="n">${s.pass_rate}%</div><div class="l">Pass rate</div></div>
    </div>
    <div class="bar">${barHtml}</div>
    <table>
      <thead><tr><th>Result</th><th>Test</th><th>Type</th><th>Time</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <p class="meta">Target: <b>${esc(d.target_url||'—')}</b> &nbsp;·&nbsp; runner: ${esc(d.runner)} &nbsp;·&nbsp; ${s.total} tests in ${ms(d.duration_ms)}</p>`;
}
</script>
</body></html>"""

# ============================================================================
# VIDEO RECORDING & BUG REPORTS (added feature)
# ============================================================================
import json as _json, subprocess as _sp, platform as _plat
from pathlib import Path as _Path
from datetime import datetime as _dt

class _ScreenRecorder:
    """Records the screen during a test run using FFmpeg (optional)."""
    def __init__(self, output_dir="./recordings", fps=15):
        self.dir = _Path(output_dir); self.dir.mkdir(parents=True, exist_ok=True)
        self.fps = fps; self.proc = None; self.file = None; self.on = False
    def start(self, name):
        ts = _dt.now().strftime("%Y%m%d_%H%M%S")
        safe = "".join(c for c in name if c.isalnum() or c in "-_").lower()
        self.file = self.dir / f"{safe}_{ts}.mp4"
        sysname = _plat.system()
        if sysname == "Windows":
            cmd = ["ffmpeg","-f","gdigrab","-framerate",str(self.fps),"-i","desktop","-c:v","libx264","-preset","ultrafast","-crf","28","-y",str(self.file)]
        elif sysname == "Darwin":
            cmd = ["ffmpeg","-f","avfoundation","-framerate",str(self.fps),"-i","1","-c:v","libx264","-preset","ultrafast","-crf","28","-y",str(self.file)]
        else:
            cmd = ["ffmpeg","-f","x11grab","-framerate",str(self.fps),"-i",":0.0","-c:v","libx264","-preset","ultrafast","-crf","28","-y",str(self.file)]
        try:
            self.proc = _sp.Popen(cmd, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
            self.on = True
            return str(self.file)
        except FileNotFoundError:
            self.on = False
            return None
    def stop(self):
        if not self.on or not self.proc: return None
        try:
            self.proc.terminate(); self.proc.wait(timeout=10); self.on = False
            if self.file.exists() and self.file.stat().st_size > 0: return str(self.file)
            return None
        except Exception:
            return None

class _BugReports:
    """Creates and lists JSON bug reports for failed tests."""
    def __init__(self, output_dir="./output"):
        self.dir = _Path(output_dir); self.dir.mkdir(parents=True, exist_ok=True)
    def create(self, test_name, failure_reason, expected, actual, video_path=None, run_id=None):
        r = {
            "id": f"BUG-{run_id}-{(test_name or 'test').replace(' ', '_').lower()}",
            "test_name": test_name, "timestamp": _dt.now().isoformat(), "status": "NEW",
            "title": f"[AUTOMATED] {test_name} failed: {failure_reason}",
            "failure_reason": failure_reason, "expected": expected, "actual": actual,
            "environment": {"os": _plat.system()},
            "artifacts": {"video": f"./recordings/{_Path(video_path).name}" if video_path else None},
            "jira_issue_key": None,
        }
        with open(self.dir / f"{r['id']}.json", "w") as f: _json.dump(r, f, indent=2)
        return r
    def list(self, run_id):
        out = []
        for fp in self.dir.glob(f"BUG-{run_id}-*.json"):
            with open(fp) as f: out.append(_json.load(f))
        return out

_recorder = _ScreenRecorder()
_bugs = _BugReports()
_active_runs: Dict[str, Any] = {}

@app.post("/runs/{run_id}/recording/start")
def rec_start(run_id: str, suite_name: str = "test_suite"):
    vp = _recorder.start(f"{suite_name}_{run_id}")
    _active_runs[run_id] = {"video_path": vp}
    return {"run_id": run_id, "recording_started": bool(vp), "video_path": vp,
            "note": None if vp else "FFmpeg not installed — recording skipped. Install FFmpeg to capture video."}

@app.post("/runs/{run_id}/recording/stop")
def rec_stop(run_id: str):
    fp = _recorder.stop()
    if run_id in _active_runs: _active_runs[run_id]["final_path"] = fp
    return {"run_id": run_id, "recording_stopped": True, "video_path": fp}

@app.post("/runs/{run_id}/bug-report")
def bug_create(run_id: str, test_data: Dict[str, Any]):
    vp = _active_runs.get(run_id, {}).get("final_path")
    return _bugs.create(test_data.get("test_name"), test_data.get("failure_reason"),
                        test_data.get("expected"), test_data.get("actual"), vp, run_id)

@app.get("/runs/{run_id}/bug-reports")
def bug_list(run_id: str):
    reports = _bugs.list(run_id)
    return {"run_id": run_id, "total_reports": len(reports), "reports": reports}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
