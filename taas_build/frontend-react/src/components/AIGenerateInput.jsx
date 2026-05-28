// AIGenerateInput.jsx
// AI Generate tab input. Works TWO ways:
//   1. URL only            -> AI reads the page, generates + validates tests
//   2. URL + a requirement -> gap analysis vs the page, generates missing tests
// The requirement is OPTIONAL. Pick "URL only" to skip it entirely.
//
// No secrets touch the frontend. For Jira/Azure the client sends only the
// ticket ID; the server holds the token and fetches the ticket.

import { useState } from "react";

const MODES = [
  { id: "url",   label: "URL only",        hint: "Just test a page — no requirement needed" },
  { id: "text",  label: "User story",      placeholder: "As a user I want to log in securely so that...\n- Given valid credentials, then I land on the dashboard\n- The account must lock after 5 failed attempts" },
  { id: "jira",  label: "Jira ticket",     placeholder: "PROJ-123" },
  { id: "azure", label: "Azure work item", placeholder: "AB#456" },
];

export default function AIGenerateInput({ onRun, onModeChange, initialMode = "url" }) {
  const DEFAULT_URL = "https://the-internet.herokuapp.com/login";
  // Each mode keeps its own URL so they don't overwrite each other.
  const [urlByMode, setUrlByMode] = useState({
    url: DEFAULT_URL, text: DEFAULT_URL, jira: DEFAULT_URL, azure: DEFAULT_URL,
  });
  const [mode, setMode] = useState(initialMode);
  const [reqByMode, setReqByMode] = useState({ text: "", jira: "", azure: "" });
  const [recordMode, setRecordMode] = useState("browser");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");

  const url = urlByMode[mode] ?? DEFAULT_URL;
  const setUrl = (v) => setUrlByMode((prev) => ({ ...prev, [mode]: v }));
  const requirement = reqByMode[mode] ?? "";
  const setRequirement = (v) => setReqByMode((prev) => ({ ...prev, [mode]: v }));

  const active = MODES.find((m) => m.id === mode);
  const needsRequirement = mode !== "url";

  async function handleRun() {
    setError("");
    if (!url.trim()) return setError("Please enter a URL to test against.");
    if (needsRequirement && !requirement.trim())
      return setError("Enter a user story or ticket ID — or switch to \u201CURL only\u201D.");

    setRunning(true);
    try {
      let res;
      if (needsRequirement) {
        // Gap-analysis flow: requirement vs the page.
        res = await fetch("/ai/generate-from-requirement", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            url: url.trim(),
            source: mode,                 // "text" | "jira" | "azure"
            requirement: requirement.trim(),
            record_mode: recordMode,
          }),
        });
      } else {
        // URL-only flow: AI reads the page and generates + runs tests.
        res = await fetch("/ai/generate-and-run", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            url: url.trim(),
            record_mode: recordMode,
          }),
        });
      }
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(
          typeof d.detail === "string" ? d.detail : d.detail?.message || `Request failed (${res.status})`
        );
      }
      onRun?.(await res.json());
    } catch (e) {
      setError(e.message || "Something went wrong.");
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-1 rounded-lg bg-[#1c1c26] border border-[#2f2f3d] p-1 w-fit">
        {MODES.map((m) => (
          <button
            key={m.id}
            onClick={() => { setMode(m.id); onModeChange?.(m.id); }}
            className={
              "px-3 py-1.5 text-sm rounded-md transition " +
              (mode === m.id
                ? "bg-[#e11d2a] text-white"
                : "text-[#cfcfda] hover:text-white hover:bg-[#23232f]")
            }
          >
            {m.label}
          </button>
        ))}
      </div>

      {needsRequirement ? (
        mode === "text" ? (
          <textarea
            value={requirement}
            onChange={(e) => setRequirement(e.target.value)}
            placeholder={active.placeholder}
            rows={5}
            className="w-full rounded-lg border border-[#3d3d4f] bg-[#0f0f15] p-3 text-sm text-[#f2f2f5] placeholder-[#6f6f80] focus:outline-none focus:ring-2 focus:ring-[#e11d2a]/40"
          />
        ) : (
          <input
            type="text"
            value={requirement}
            onChange={(e) => setRequirement(e.target.value)}
            placeholder={active.placeholder}
            className="w-full rounded-lg border border-[#3d3d4f] bg-[#0f0f15] px-3 py-2 text-sm text-[#f2f2f5] placeholder-[#6f6f80] focus:outline-none focus:ring-2 focus:ring-[#e11d2a]/40"
          />
        )
      ) : (
        <p className="text-sm text-[#9a9aac]">
          {active.hint}. TaaS reads the page, generates test cases, runs them in
          a real browser, and validates the results.
        </p>
      )}

      {needsRequirement && mode !== "text" && (
        <p className="text-xs text-[#9a9aac]">
          The ticket is fetched securely on the server using a Personal Access
          Token. Your credentials never reach the browser.
        </p>
      )}

      <div className="flex flex-col sm:flex-row gap-2">
        <input
          type="text"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="https://your-app.com/login"
          className="flex-1 rounded-lg border border-[#3d3d4f] bg-[#0f0f15] px-3 py-2 text-sm text-[#f2f2f5] placeholder-[#6f6f80] focus:outline-none focus:ring-2 focus:ring-[#e11d2a]/40"
        />
        <select
          value={recordMode}
          onChange={(e) => setRecordMode(e.target.value)}
          className="rounded-lg border border-[#3d3d4f] bg-[#0f0f15] px-3 py-2 text-sm text-[#f2f2f5]"
        >
          <option value="browser">Record: browser only</option>
          <option value="screen">Record: full screen</option>
          <option value="both">Record: both</option>
        </select>
        <button
          onClick={handleRun}
          disabled={running}
          className="rounded-lg bg-[#e11d2a] px-5 py-2 text-sm font-medium text-white hover:bg-[#c0151f] disabled:opacity-60"
        >
          {running ? "Working\u2026" : needsRequirement ? "\u2728 Analyze gaps & generate" : "\u2728 Generate & run"}
        </button>
      </div>

      {error && <p className="text-sm text-[#f87171]">{error}</p>}
    </div>
  );
}
