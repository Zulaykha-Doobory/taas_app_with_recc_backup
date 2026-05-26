// UploadTab.jsx — download Excel/CSV templates, then upload a filled file to run.
// Wires to existing backend: /templates/excel, /templates/csv, /upload/run.
import { useState, useRef } from "react";

export default function UploadTab({ onRun }) {
  const [file, setFile] = useState(null);
  const [dragOver, setDragOver] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const inputRef = useRef(null);

  function pick(f) {
    setError("");
    if (!f) return;
    const ok = /\.(xlsx|xls|csv)$/i.test(f.name);
    if (!ok) {
      setError("Please choose an .xlsx, .xls or .csv file.");
      return;
    }
    setFile(f);
  }

  async function run() {
    if (!file) {
      setError("Choose a file first, or download a template to fill in.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const fd = new FormData();
      fd.append("file", file);
      const r = await fetch("/upload/run", { method: "POST", body: fd });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        throw new Error(
          typeof d.detail === "string"
            ? d.detail
            : d.detail?.message || `Upload failed (${r.status})`
        );
      }
      onRun?.(await r.json());
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-5">
      <p className="text-sm text-[#cfcfda]">
        Already have test cases in a spreadsheet? Download a template, fill in
        your steps, and upload it — TaaS runs them straight away.
      </p>

      {/* Template downloads */}
      <div className="rounded-xl bg-[#1c1c26] border border-[#2f2f3d] p-4">
        <div className="text-sm font-medium text-[#f2f2f5] mb-1">
          1 · Get a template
        </div>
        <p className="text-xs text-[#9a9aac] mb-3">
          The Excel version has dropdowns and an instructions sheet. The CSV is
          plain and works anywhere.
        </p>
        <div className="flex flex-wrap gap-2">
          <a
            href="/templates/excel"
            download
            className="rounded-lg border border-[#3d3d4f] px-4 py-2 text-sm text-[#f2f2f5] hover:bg-[#23232f] no-underline"
          >
            ⬇ Download Excel template
          </a>
          <a
            href="/templates/csv"
            download
            className="rounded-lg border border-[#3d3d4f] px-4 py-2 text-sm text-[#f2f2f5] hover:bg-[#23232f] no-underline"
          >
            ⬇ Download CSV template
          </a>
        </div>
      </div>

      {/* Upload */}
      <div className="rounded-xl bg-[#1c1c26] border border-[#2f2f3d] p-4">
        <div className="text-sm font-medium text-[#f2f2f5] mb-3">
          2 · Upload your filled file
        </div>

        <div
          onDragOver={(e) => {
            e.preventDefault();
            setDragOver(true);
          }}
          onDragLeave={() => setDragOver(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragOver(false);
            pick(e.dataTransfer.files?.[0]);
          }}
          onClick={() => inputRef.current?.click()}
          className={
            "rounded-lg border-2 border-dashed p-6 text-center cursor-pointer transition " +
            (dragOver
              ? "border-[#e11d2a] bg-[#e11d2a]/5"
              : "border-[#3d3d4f] hover:border-[#4d4d61]")
          }
        >
          <input
            ref={inputRef}
            type="file"
            accept=".xlsx,.xls,.csv"
            className="hidden"
            onChange={(e) => pick(e.target.files?.[0])}
          />
          {file ? (
            <div className="text-sm text-[#f2f2f5]">
              <span className="text-[#4ade80]">✓</span> {file.name}
              <div className="text-xs text-[#9a9aac] mt-1">
                Click to choose a different file
              </div>
            </div>
          ) : (
            <div className="text-sm text-[#cfcfda]">
              Drag a file here, or click to browse
              <div className="text-xs text-[#9a9aac] mt-1">
                .xlsx, .xls or .csv
              </div>
            </div>
          )}
        </div>

        <button
          onClick={run}
          disabled={busy}
          className="mt-3 rounded-lg bg-[#e11d2a] px-5 py-2 text-sm font-medium text-white hover:bg-[#c0151f] disabled:opacity-60"
        >
          {busy ? "Running…" : "▶ Upload & run tests"}
        </button>

        {error && <p className="text-sm text-[#f87171] mt-2">{error}</p>}
      </div>
    </div>
  );
}
