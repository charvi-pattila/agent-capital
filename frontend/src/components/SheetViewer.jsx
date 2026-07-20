import { useState, useEffect } from "react";

const MAX_ROWS = 1000;
const MAX_COLS = 256;

// ── Excel color resolution ────────────────────────────────────────────────────

// Legacy indexed palette (BIFF8 standard)
const INDEXED = [
  "000000", "FFFFFF", "FF0000", "00FF00", "0000FF", "FFFF00", "FF00FF", "00FFFF",
  "000000", "FFFFFF", "FF0000", "00FF00", "0000FF", "FFFF00", "FF00FF", "00FFFF",
  "800000", "008000", "000080", "808000", "800080", "008080", "C0C0C0", "808080",
  "9999FF", "993366", "FFFFCC", "CCFFFF", "660066", "FF8080", "0066CC", "CCCCFF",
  "000080", "FF00FF", "FFFF00", "00FFFF", "800080", "800000", "008080", "0000FF",
  "00CCFF", "CCFFFF", "CCFFCC", "FFFF99", "99CCFF", "FF99CC", "CC99FF", "FFCC99",
  "3366FF", "33CCCC", "99CC00", "FFCC00", "FF9900", "FF6600", "666699", "969696",
  "003366", "339966", "003300", "333300", "993300", "993366", "333399", "333333",
];

function applyTint(hex, tint) {
  if (!tint) return "#" + hex;
  const ch = [0, 2, 4].map(i => parseInt(hex.slice(i, i + 2), 16)).map(v =>
    Math.max(0, Math.min(255, Math.round(tint < 0 ? v * (1 + tint) : v + (255 - v) * tint)))
  );
  return "#" + ch.map(v => v.toString(16).padStart(2, "0")).join("");
}

// Theme color slots in the order Excel's theme indices reference them
// (0=lt1, 1=dk1, 2=lt2, 3=dk2, 4-9=accent1-6, 10=hlink, 11=folHlink)
const THEME_SLOTS = ["lt1", "dk1", "lt2", "dk2", "accent1", "accent2", "accent3", "accent4", "accent5", "accent6", "hlink", "folHlink"];

function themePalette(xml) {
  if (!xml) return [];
  try {
    const doc = new DOMParser().parseFromString(xml, "application/xml");
    const scheme = doc.getElementsByTagName("a:clrScheme")[0];
    if (!scheme) return [];
    const byName = {};
    for (const el of scheme.children) {
      const color = el.firstElementChild;
      if (!color) continue;
      byName[el.localName] = color.localName === "srgbClr"
        ? color.getAttribute("val")
        : (color.getAttribute("lastClr") || (color.getAttribute("val") === "window" ? "FFFFFF" : "000000"));
    }
    return THEME_SLOTS.map(s => byName[s] || null);
  } catch (_) {
    return [];
  }
}

function resolveColor(c, palette) {
  if (!c) return null;
  if (c.argb) return "#" + c.argb.slice(-6);
  if (typeof c.indexed === "number") return INDEXED[c.indexed] ? "#" + INDEXED[c.indexed] : null;
  if (typeof c.theme === "number" && palette[c.theme]) return applyTint(palette[c.theme], c.tint || 0);
  return null;
}

// ── Workbook → render model ──────────────────────────────────────────────────

function cellCss(cell, palette, isNumber) {
  const st = {};
  const f = cell.font;
  if (f) {
    if (f.bold) st.fontWeight = "bold";
    if (f.italic) st.fontStyle = "italic";
    const deco = [f.underline && "underline", f.strike && "line-through"].filter(Boolean).join(" ");
    if (deco) st.textDecoration = deco;
    const fc = resolveColor(f.color, palette);
    if (fc) st.color = fc;
    if (f.size && f.size !== 11) st.fontSize = Math.round(f.size * 4 / 3) + "px";
    if (f.name) st.fontFamily = `"${f.name}", Calibri, "Segoe UI", sans-serif`;
  }
  if (cell.fill?.type === "pattern" && cell.fill.pattern === "solid") {
    const bg = resolveColor(cell.fill.fgColor, palette);
    if (bg) st.background = bg;
  }
  const al = cell.alignment;
  if (al?.horizontal) st.textAlign = al.horizontal;
  else if (isNumber) st.textAlign = "right"; // Excel default for numbers/dates
  if (al?.vertical && al.vertical !== "bottom") st.verticalAlign = al.vertical === "middle" ? "middle" : al.vertical;
  if (al?.wrapText) st.whiteSpace = "pre-wrap";
  const b = cell.border;
  if (b) {
    const side = (s) => {
      if (!s?.style) return null;
      const w = s.style === "medium" || s.style === "thick" ? 2 : 1;
      return `${w}px solid ${resolveColor(s.color, palette) || "#9a9a9a"}`;
    };
    const t = side(b.top), r = side(b.right), bo = side(b.bottom), l = side(b.left);
    if (t) st.borderTop = t;
    if (r) st.borderRight = r;
    if (bo) st.borderBottom = bo;
    if (l) st.borderLeft = l;
  }
  return st;
}

function buildSheet(ws, sjsSheet, palette, utils) {
  const mergeMap = {};
  const covered = new Set();
  for (const m of ws.model.merges || []) {
    const r = utils.decode_range(m);
    mergeMap[`${r.s.r},${r.s.c}`] = { colspan: r.e.c - r.s.c + 1, rowspan: r.e.r - r.s.r + 1 };
    for (let i = r.s.r; i <= r.e.r; i++) {
      for (let j = r.s.c; j <= r.e.c; j++) {
        if (i !== r.s.r || j !== r.s.c) covered.add(`${i},${j}`);
      }
    }
  }

  const totalRows = ws.rowCount;
  const numRows = Math.min(totalRows, MAX_ROWS);
  const numCols = Math.min(Math.max(ws.columnCount, 1), MAX_COLS);

  const cols = [];
  for (let c = 1; c <= numCols; c++) {
    const col = ws.getColumn(c);
    cols.push({
      width: Math.round((col.width || 8.43) * 7 + 5), // Excel char units → px
      hidden: !!col.hidden,
    });
  }

  const rows = [];
  for (let r = 1; r <= numRows; r++) {
    const row = ws.getRow(r);
    if (row.hidden) continue;
    const cells = [];
    for (let c = 1; c <= numCols; c++) {
      if (covered.has(`${r - 1},${c - 1}`)) continue;
      const cell = row.getCell(c);
      const sjs = sjsSheet?.[utils.encode_cell({ r: r - 1, c: c - 1 })];
      const text = sjs ? (sjs.w ?? (sjs.v != null ? String(sjs.v) : "")) : "";
      const merge = mergeMap[`${r - 1},${c - 1}`];
      cells.push({
        key: c,
        text,
        colspan: merge?.colspan,
        rowspan: merge?.rowspan,
        style: cellCss(cell, palette, sjs?.t === "n"),
      });
    }
    rows.push({ key: r, height: row.height ? Math.round(row.height * 4 / 3) : null, cells });
  }

  return {
    name: ws.name,
    cols,
    rows,
    totalRows,
    gridLines: ws.views?.[0]?.showGridLines !== false,
  };
}

async function loadStyledSheets(buf) {
  const [excelMod, XLSX] = await Promise.all([import("exceljs"), import("xlsx")]);
  const ExcelJS = excelMod.default || excelMod;
  const wb = new ExcelJS.Workbook();
  await wb.xlsx.load(buf);
  const sjs = XLSX.read(buf, { type: "array" }); // provides Excel-formatted display text (.w)
  const palette = themePalette(wb.model?.themes?.theme1);
  return wb.worksheets
    .filter(ws => !ws.state || ws.state === "visible")
    .map(ws => buildSheet(ws, sjs.Sheets[ws.name], palette, XLSX.utils));
}

// ── Plain fallback (csv/tsv and legacy .xls — no style info available) ───────

function parseDelimited(text, delim) {
  const rows = [];
  let row = [];
  let field = "";
  let inQuotes = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (inQuotes) {
      if (c === '"') {
        if (text[i + 1] === '"') { field += '"'; i++; }
        else inQuotes = false;
      } else field += c;
    } else if (c === '"') inQuotes = true;
    else if (c === delim) { row.push(field); field = ""; }
    else if (c === "\n") { row.push(field); field = ""; rows.push(row); row = []; }
    else if (c !== "\r") field += c;
  }
  if (field !== "" || row.length) { row.push(field); rows.push(row); }
  return rows;
}

function PlainTable({ rows }) {
  if (!rows.length) return <div className="preview-center">Empty sheet</div>;
  const [head, ...body] = rows;
  const shown = body.slice(0, MAX_ROWS);
  let cols = head.length;
  for (const r of shown) if (r.length > cols) cols = r.length;
  const idx = Array.from({ length: cols }, (_, i) => i);

  return (
    <>
      <table className="preview-table">
        <thead>
          <tr>{idx.map(i => <th key={i}>{String(head[i] ?? "")}</th>)}</tr>
        </thead>
        <tbody>
          {shown.map((r, i) => (
            <tr key={i}>{idx.map(j => <td key={j}>{String(r[j] ?? "")}</td>)}</tr>
          ))}
        </tbody>
      </table>
      {body.length > MAX_ROWS && (
        <div className="table-note">Showing first {MAX_ROWS} of {body.length} rows</div>
      )}
    </>
  );
}

// ── Styled renderer ───────────────────────────────────────────────────────────

function StyledSheet({ sheet }) {
  return (
    <>
      <table className={"xlsx-table" + (sheet.gridLines ? " grid" : "")}>
        <colgroup>
          {sheet.cols.map((c, i) => (
            <col
              key={i}
              style={c.hidden
                ? { visibility: "collapse" }
                : { width: c.width + "px", minWidth: c.width + "px" }}
            />
          ))}
        </colgroup>
        <tbody>
          {sheet.rows.map(row => (
            <tr key={row.key} style={row.height ? { height: row.height + "px" } : undefined}>
              {row.cells.map(cell => (
                <td key={cell.key} colSpan={cell.colspan} rowSpan={cell.rowspan} style={cell.style}>
                  {cell.text}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {sheet.totalRows > MAX_ROWS && (
        <div className="table-note" style={{ color: "#888" }}>
          Showing first {MAX_ROWS} of {sheet.totalRows} rows
        </div>
      )}
    </>
  );
}

// ── Main viewer ───────────────────────────────────────────────────────────────

export default function SheetViewer({ url, ext }) {
  const [state, setState] = useState({ status: "loading" });
  const [active, setActive] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading" });
    setActive(0);
    (async () => {
      try {
        const r = await fetch(url);
        if (!r.ok) throw new Error();
        if (ext === "csv" || ext === "tsv") {
          const rows = parseDelimited(await r.text(), ext === "tsv" ? "\t" : ",");
          if (!cancelled) setState({ status: "ready", plain: true, sheets: [{ name: "Sheet", rows }] });
        } else if (ext === "xlsx" || ext === "xlsm") {
          const sheets = await loadStyledSheets(await r.arrayBuffer());
          if (!cancelled) setState({ status: "ready", sheets });
        } else {
          const XLSX = await import("xlsx");
          const wb = XLSX.read(await r.arrayBuffer(), { type: "array" });
          const sheets = wb.SheetNames.map(n => ({
            name: n,
            rows: XLSX.utils.sheet_to_json(wb.Sheets[n], { header: 1, raw: false, defval: "" }),
          }));
          if (!cancelled) setState({ status: "ready", plain: true, sheets });
        }
      } catch (_) {
        if (!cancelled) setState({ status: "error" });
      }
    })();
    return () => { cancelled = true; };
  }, [url, ext]);

  if (state.status === "error") {
    return <div className="preview-body"><div className="preview-center">Could not load spreadsheet</div></div>;
  }
  if (state.status === "loading") {
    return <div className="preview-body"><div className="preview-center">Loading…</div></div>;
  }

  const { sheets, plain } = state;
  const sheet = sheets[Math.min(active, sheets.length - 1)];

  return (
    <>
      {sheets.length > 1 && (
        <div className="sheet-tabs">
          {sheets.map((s, i) => (
            <button
              key={s.name}
              className={"sheet-tab" + (i === active ? " active" : "")}
              onClick={() => setActive(i)}
            >{s.name}</button>
          ))}
        </div>
      )}
      <div className={"preview-body" + (plain ? "" : " light")}>
        {plain ? <PlainTable rows={sheet.rows} /> : <StyledSheet sheet={sheet} />}
      </div>
    </>
  );
}
