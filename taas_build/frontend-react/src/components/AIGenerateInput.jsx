// AIGenerateInput.jsx
// The new "AI Generate" input component.
// Accepts a URL plus a requirement source: free text, a Jira ticket, or an
// Azure DevOps work item. Sends everything to the backend, which fetches the
// ticket (PAT auth, server-side), runs gap analysis, and generates the
// missing tests.
//
// Tailwind for styling. No secrets ever touch the frontend — the PATs live in
// the server's environment. The client only ever sends the ticket *ID*.

import { useState } from "react";

const SOURCE_TABS = [
  { id: "text",  label: "User story",   placeholder: "As a user I want to log in securely so that...\n- Given valid credentials, then I land on the dashboard\n- The account must lock after 5 failed attempts" },
  { id: "jira",  label: "Jira ticket",  placeholder: "PROJ-123" },
  { id: "azure", label: "Azure work item", placeholder: "AB#456" },
];

export default function AIGenerateInput({ onRun }) {
  const [url, setUrl] = useState("https://the-internet.herokuapp.com/login");
  const [source, setSource] = useState("text");
  const [requirement, setRequirement] = useState("");
  const [recordMode, setRecordMode] = useState("browser");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");

  const active = SOURCE_TABS.find((t) => t.id === source);

  async function handleRun() {
    setError("");
    if (!url.trim()) return setError("Please enter a URL to test against.");
    if (!requirement.trim())
      return setError("Please paste a user story or enter a ticket ID.");

    setRunning(true);
    try {
      // The backend decides text vs jira vs azure from `source`,
      // fetches the ticket server-side, runs gap analysis, generates + runs.
      const res = await fetch("/ai/generate-from-requirement", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url: url.trim(),
          source,                      // "text" | "jira" | "azure"
          requirement: requirement.trim(),
          record_mode: recordMode,     // "browser" | "screen" | "both"
        }),
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        throw new Error(detail?.detail?.message || `Request failed (${res.status})`);
      }
      const data = await res.json();
      onRun?.(data);                   // hand results up to the dashboard
    } catch (e) {
      setError(e.message || "Something went wrong.");
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className="space-y-4">
      {/* Source selector */}
      <div className="flex gap-1 rounded-lg bg-neutral-100 p-1 w-fit">
        {SOURCE_TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setSource(t.id)}
            className={
              "px-3 py-1.5 text-sm rounded-md transition " +
              (source === t.id
                ? "bg-white shadow-sm text-neutral-900"
                : "text-neutral-500 hover:text-neutral-800")
            }
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* Requirement input — textarea for text, single line for tickets */}
      {source === "text" ? (
        <textarea
          value={requirement}
          onChange={(e) => setRequirement(e.target.value)}
          placeholder={active.placeholder}
          rows={5}
          className="w-full rounded-lg border border-neutral-300 bg-white p-3 text-sm
                     focus:outline-none focus:ring-2 focus:ring-red-500/40"
        />
      ) : (
        <input
          type="text"
          value={requirement}
          onChange={(e) => setRequirement(e.target.value)}
          placeholder={active.placeholder}
          className="w-full rounded-lg border border-neutral-300 bg-white px-3 py-2 text-sm
                     focus:outline-none focus:ring-2 focus:ring-red-500/40"
        />
      )}
      {source !== "text" && (
        <p className="text-xs text-neutral-500">
          The ticket is fetched securely on the server using a Personal Access
          Token. Your credentials never reach the browser.
        </p>
      )}

      {/* Target URL + record mode + run */}
      <div className="flex flex-col sm:flex-row gap-2">
        <input
          type="text"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="https://your-app.com/login"
          className="flex-1 rounded-lg border border-neutral-300 bg-white px-3 py-2 text-sm
                     focus:outline-none focus:ring-2 focus:ring-red-500/40"
        />
        <select
          value={recordMode}
          onChange={(e) => setRecordMode(e.target.value)}
          className="rounded-lg border border-neutral-300 bg-white px-3 py-2 text-sm"
        >
          <option value="browser">Record: browser only</option>
          <option value="screen">Record: full screen</option>
          <option value="both">Record: both</option>
        </select>
        <button
          onClick={handleRun}
          disabled={running}
          className="rounded-lg bg-red-600 px-5 py-2 text-sm font-medium text-white
                     hover:bg-red-700 disabled:opacity-60"
        >
          {running ? "Analyzing & running…" : "✨ Analyze gaps & generate"}
        </button>
      </div>

      {error && <p className="text-sm text-red-600">{error}</p>}
    </div>
  );
}
