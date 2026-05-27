import { useState, useEffect } from "react";
import AIGenerateInput from "./components/AIGenerateInput.jsx";
import CoverageBar from "./components/CoverageBar.jsx";
import TestRow from "./components/TestRow.jsx";
import UploadTab from "./components/UploadTab.jsx";

const TABS = [
  { id: "demo", label: "\u25B6 Demo" },
  { id: "live", label: "Live site" },
  { id: "upload", label: "\u2B06 Upload file" },
  { id: "ai", label: "\u2728 AI Generate" },
];

export default function App() {
  const [tab, setTab] = useState("ai");
  // results are stored PER TAB so each tab keeps its own and they never
  // bleed across tabs. e.g. { ai: {...}, upload: {...} }
  const [resultsByTab, setResultsByTab] = useState({});
  const [loading, setLoading] = useState(false);
  const [ai, setAi] = useState({ running: false, model: null });

  const result = resultsByTab[tab] || null;
  const setResultForTab = (data) =>
    setResultsByTab((prev) => ({ ...prev, [tab]: data }));

  useEffect(() => {
    fetch("/ai/status")
      .then((r) => r.json())
      .then((d) => setAi({ running: d.ollama_running, model: d.configured_model }))
      .catch(() => {});
  }, []);

  async function runSimple(endpoint) {
    setLoading(true);
    setResultForTab(null);
    try {
      const r = await fetch(endpoint, { method: "POST" });
      setResultForTab(await r.json());
    } finally {
      setLoading(false);
    }
  }

  const summary = result?.summary;

  return (
    <div className="min-h-screen px-6 py-6 max-w-5xl mx-auto text-[#cfcfda]">
      <header className="mb-6">
        <h1 className="text-xl font-semibold text-[#f2f2f5]">
          TaaS &mdash; QA Automation Platform
        </h1>
        <p className="text-sm text-[#9a9aac]">
          Requirement-driven test generation &middot; real browser execution
        </p>
      </header>

      <nav className="flex gap-1 mb-6 bg-[#1c1c26] p-1 rounded-lg w-fit border border-[#2f2f3d]">
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={
              "px-4 py-2 text-sm rounded-md transition " +
              (tab === t.id
                ? "bg-[#e11d2a] text-white"
                : "text-[#cfcfda] hover:text-white hover:bg-[#23232f]")
            }
          >
            {t.label}
          </button>
        ))}
      </nav>

      {tab === "ai" && (
        <div className="mb-6">
          <div className="flex items-center gap-2 mb-4 text-sm">
            <span
              className={"inline-block w-2 h-2 rounded-full " + (ai.running ? "bg-[#4ade80]" : "bg-[#fbbf24]")}
            />
            <span className="text-[#cfcfda]">
              {ai.running
                ? `Ollama running \u00B7 ${ai.model} \u00B7 ready`
                : "Ollama not detected \u2014 structure-based generation will be used"}
            </span>
          </div>
          <AIGenerateInput onRun={(data) => setResultForTab(data)} />
        </div>
      )}

      {tab === "upload" && (
        <div className="mb-6">
          <UploadTab onRun={(data) => setResultForTab(data)} />
        </div>
      )}

      {tab === "live" && (
        <div className="mb-6">
          <p className="text-sm text-[#cfcfda] mb-3">
            Runs the real suite against the-internet.herokuapp.com in a real
            browser, recording and flagging failures automatically.
          </p>
          <button
            onClick={() => runSimple("/run/live?record_mode=browser")}
            className="rounded-lg bg-[#e11d2a] px-5 py-2 text-sm font-medium text-white hover:bg-[#c0151f]"
          >
            &#9654; Run live suite
          </button>
        </div>
      )}

      {tab === "demo" && (
        <div className="mb-6">
          <p className="text-sm text-[#cfcfda] mb-3">
            A quick canned demonstration of how results, recordings, and bug
            reports look.
          </p>
          <button
            onClick={() => runSimple("/run/demo")}
            className="rounded-lg bg-[#e11d2a] px-5 py-2 text-sm font-medium text-white hover:bg-[#c0151f]"
          >
            &#9654; Run demo
          </button>
        </div>
      )}

      {loading && <p className="text-[#cfcfda] text-sm">Running tests&hellip;</p>}

      {result && (
        <section>
          <CoverageBar
            coverage={result.coverage}
            covered={result.covered}
            gaps={result.gaps}
            source={result.requirement?.source}
          />

          {summary && (
            <div className="grid grid-cols-5 gap-3 mb-5">
              {[
                ["Total", summary.total, "text-[#f2f2f5]"],
                ["Passed", summary.passed, "text-[#4ade80]"],
                ["Failed", summary.failed, "text-[#f87171]"],
                ["Errored", summary.error, "text-[#fbbf24]"],
                ["Pass rate", summary.total ? Math.round((summary.passed / summary.total) * 100) + "%" : "\u2014", "text-[#f2f2f5]"],
              ].map(([label, val, cls]) => (
                <div key={label} className="rounded-lg bg-[#1c1c26] border border-[#2f2f3d] p-3">
                  <div className={"text-2xl font-semibold " + cls}>{val}</div>
                  <div className="text-xs text-[#9a9aac]">{label}</div>
                </div>
              ))}
            </div>
          )}

          {result.note && <p className="text-xs text-[#fbbf24] mb-3">{result.note}</p>}

          <div className="flex flex-col gap-2">
            {(result.cases || []).map((t, i) => (
              <TestRow key={i} test={t} runId={result.run_id} videoPath={result.video_path} />
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
