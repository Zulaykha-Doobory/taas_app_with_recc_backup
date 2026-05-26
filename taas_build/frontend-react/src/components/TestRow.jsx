// TestRow.jsx — one result row with persistent artifact buttons.
// Recording (modal video), Screenshots (gallery), Bug report (summary).
// This replaces the old expanded list + separate Recordings/Bug Reports tabs.
import { useState } from "react";

function Modal({ title, onClose, children }) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={onClose}
    >
      <div
        className="w-full max-w-2xl rounded-xl bg-[#1c1c26] border border-[#3d3d4f] p-5"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-base font-medium text-[#f2f2f5]">{title}</h3>
          <button
            onClick={onClose}
            className="text-[#cfcfda] hover:text-[#f2f2f5] text-lg leading-none"
          >
            ×
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

export default function TestRow({ test, runId, videoPath }) {
  const [modal, setModal] = useState(null); // "rec" | "shots" | "bug" | null
  const failed = test.status === "failed" || test.status === "error";

  const statusStyle = failed
    ? "text-red-400 bg-red-500/10"
    : test.status === "skipped"
    ? "text-[#cfcfda] bg-neutral-500/10"
    : "text-green-400 bg-green-500/10";

  const borderStyle = failed ? "border-red-500/40" : "border-[#2f2f3d]";

  return (
    <>
      <div
        className={`flex items-center gap-3 rounded-lg bg-[#1c1c26] border ${borderStyle} px-3.5 py-2.5`}
      >
        <span
          className={`text-xs font-medium px-2 py-1 rounded-md min-w-[54px] text-center ${statusStyle}`}
        >
          {test.status}
        </span>

        <div className="flex-1 min-w-0">
          <div className="text-sm text-[#f2f2f5] truncate">
            {test.name}
            {test.source?.startsWith("gap:") && (
              <span className="ml-2 text-[11px] text-sky-300 bg-sky-500/10 px-1.5 py-0.5 rounded">
                gap
              </span>
            )}
          </div>
          <div className="text-xs text-[#9a9aac]">
            {test.category} · {(test.duration_ms / 1000).toFixed(1)}s
            {failed && test.failure_reason ? ` · ${test.failure_reason}` : ""}
          </div>
        </div>

        <button
          onClick={() => setModal("rec")}
          className="text-xs px-2.5 py-1.5 rounded-md border border-[#3d3d4f] hover:bg-[#23232f]"
        >
          ▶ Recording
        </button>
        <button
          onClick={() => setModal("shots")}
          className="text-xs px-2.5 py-1.5 rounded-md border border-[#3d3d4f] hover:bg-[#23232f]"
        >
          ▦ Screens
        </button>
        {failed && (
          <button
            onClick={() => setModal("bug")}
            className="text-xs px-2.5 py-1.5 rounded-md border border-red-500/50 text-red-300 hover:bg-red-500/10"
          >
            🐞 Bug report
          </button>
        )}
      </div>

      {modal === "rec" && (
        <Modal title={`Recording — ${test.name}`} onClose={() => setModal(null)}>
          {videoPath ? (
            <video
              src={`/${videoPath}`}
              controls
              className="w-full rounded-lg bg-black"
            />
          ) : (
            <p className="text-sm text-[#cfcfda]">
              No recording was captured for this run.
            </p>
          )}
        </Modal>
      )}

      {modal === "shots" && (
        <Modal title={`Screenshots — ${test.name}`} onClose={() => setModal(null)}>
          {test.screenshot ? (
            <img
              src={`/${test.screenshot}`}
              alt="failure screenshot"
              className="w-full rounded-lg border border-[#3d3d4f]"
            />
          ) : (
            <p className="text-sm text-[#cfcfda]">
              No screenshots captured for this test.
            </p>
          )}
        </Modal>
      )}

      {modal === "bug" && (
        <Modal title="Bug report" onClose={() => setModal(null)}>
          <div className="text-sm text-[#cfcfda] space-y-2">
            <p>
              <span className="text-[#9a9aac]">Title: </span>
              [AUTOMATED] {test.name} failed
            </p>
            <p>
              <span className="text-[#9a9aac]">Reason: </span>
              {test.failure_reason || "Test failed"}
            </p>
            <p>
              <span className="text-[#9a9aac]">Stored at: </span>
              <code className="text-xs">output/BUG-{runId}-…json</code>
            </p>
            <p className="text-[#9a9aac] text-xs pt-1">
              jira_issue_key is null — this report is ready to push to Jira
              (US-5.2).
            </p>
          </div>
        </Modal>
      )}
    </>
  );
}
