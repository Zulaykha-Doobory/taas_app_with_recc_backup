// App.jsx — TaaS dashboard root.
// Tabs: Demo · Live site · Upload · AI Generate.
// Recordings and Bug Reports are NO LONGER tabs — their artifacts live
// inside each test row (see TestRow.jsx).
import { useState, useEffect } from "react";
import AIGenerateInput from "./components/AIGenerateInput.jsx";
import CoverageBar from "./components/CoverageBar.jsx";
import TestRow from "./components/TestRow.jsx";

const TABS = [
  { id: "demo", label: "▶ Demo" },
  { id: "live", label: "Live site" },
  { id: "upload", label: "⇧ Upload file" },
  { id: "ai", label: "✨ AI Generate" },
];

export default function App() {
  const [tab, setTab] = useState("ai");
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [ai, setAi] = useState({ running: false, model: null });

  useEffect(() => {
    fetch("/ai/status")
      .then((r) => r.json())
      .then((d) => setAi({ running: d.ollama_running, model: d.configured_model }))
      .catch(() => {});
  }, []);

  async function runSimple(endpoint) {
    setLoading(true);
    setResult(null);
    try {
      const r = await fetch(endpoint, { method: "POST" });
      setResult(await r.json());
    } finally {
      setLoading(false);
    }
  }

  const summary = result?.summary;

  return (
    <div className="min-h-screen px-6 py-6 max-w-5xl mx-auto">
      <header className="mb-6">
        <h1 className="text-xl font-semibold text-white">
          TaaS — QA Automation Platform
        </h1>
        <p className="text-sm text-neutral-400">
          Requirement-driven test generation · real browser execution
        </p>
      </header>

      <nav className="flex gap-1 mb-6 bg-neutral-900/60 p-1 rounded-lg w-fit">
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={
              "px-4 py-2 text-sm rounded-md transition " +
              (tab === t.id
                ? "bg-[#cc0011] text-white"
                : "text-neutral-400 hover:text-neutral-100")
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
              className={`inline-block w-2 h-2 rounded-full ${
                ai.running ? "bg-green-500" : "bg-amber-500"
              }`}
            />
            <span className="text-neutral-400">
              {ai.running
                ? `Ollama running · ${ai.model} · ready`
                : "Ollama not detected — structure-based generation will be used"}
            </span>
          </div>
          <AIGenerateInput
            onRun={(data) => setResult(data)}
          />
        </div>
      )}

      {tab === "live" && (
        <div className="mb-6">
          <p className="text-sm text-neutral-400 mb-3">
            Runs the real suite against the-internet.herokuapp.com in a real
            browser, recording and flagging failures automatically.
          </p>
          <button
            onClick={() => runSimple("/run/live?record_mode=browser")}
            className="rounded-lg bg-[#cc0011] px-5 py-2 text-sm font-medium text-white hover:bg-[#a8000e]"
          >
            ▶ Run live suite
          </button>
        </div>
      )}

      {tab === "demo" && (
        <div className="mb-6">
          <button
            onClick={() => runSimple("/run/demo")}
            className="rounded-lg bg-[#cc0011] px-5 py-2 text-sm font-medium text-white hover:bg-[#a8000e]"
          >
            ▶ Run demo
          </button>
        </div>
      )}

      {tab === "upload" && (
        <div className="mb-6 text-sm text-neutral-400">
          Upload tab — download the template, fill it in, and upload to run your
          own tests. (Wired to existing /upload and /templates endpoints.)
        </div>
      )}

      {loading && (
        <p className="text-neutral-400 text-sm">Running tests…</p>
      )}

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
                ["Total", summary.total, "text-neutral-100"],
                ["Passed", summary.passed, "text-green-400"],
                ["Failed", summary.failed, "text-red-400"],
                ["Errored", summary.error, "text-amber-400"],
                [
                  "Pass rate",
                  summary.total
                    ? Math.round((summary.passed / summary.total) * 100) + "%"
                    : "—",
                  "text-neutral-100",
                ],
              ].map(([label, val, cls]) => (
                <div
                  key={label}
                  className="rounded-lg bg-neutral-900/60 border border-neutral-800 p-3"
                >
                  <div className={`text-2xl font-semibold ${cls}`}>{val}</div>
                  <div className="text-xs text-neutral-500">{label}</div>
                </div>
              ))}
            </div>
          )}

          {result.note && (
            <p className="text-xs text-amber-300/80 mb-3">{result.note}</p>
          )}

          <div className="flex flex-col gap-2">
            {(result.cases || []).map((t, i) => (
              <TestRow
                key={i}
                test={t}
                runId={result.run_id}
                videoPath={result.video_path}
              />
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
