// theme.js — shared color + style tokens.
// Higher-contrast dark palette so muted text stays readable.
// Import these instead of scattering raw Tailwind grays everywhere.

export const T = {
  // surfaces
  page: "bg-[#13131a]",
  card: "bg-[#1c1c26]",
  cardHover: "hover:bg-[#23232f]",
  inset: "bg-[#0f0f15]",

  // borders
  border: "border-[#2f2f3d]",
  borderStrong: "border-[#3d3d4f]",

  // text — brighter than the old neutral-400/500
  textPrimary: "text-[#f2f2f5]",
  textBody: "text-[#cfcfda]",     // was neutral-300/400 — now clearly readable
  textMuted: "text-[#9a9aac]",    // was neutral-500 — lifted for contrast

  // brand
  brand: "bg-[#e11d2a]",          // a touch brighter than #cc0011 for visibility
  brandHover: "hover:bg-[#c0151f]",

  // status (accessible on dark bg)
  okText: "text-[#4ade80]",
  okBg: "bg-[#4ade80]/15",
  failText: "text-[#f87171]",
  failBg: "bg-[#f87171]/15",
  warnText: "text-[#fbbf24]",
  warnBg: "bg-[#fbbf24]/15",
  infoText: "text-[#60a5fa]",
  infoBg: "bg-[#60a5fa]/15",
};
