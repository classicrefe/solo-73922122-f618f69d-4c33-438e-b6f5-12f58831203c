"use strict";

const $ = (sel) => document.querySelector(sel);

const state = {
  lastBox: null,
};

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const text = await response.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { message: text };
    }
  }
  if (!response.ok) {
    const err = new Error((data && (data.message || data.error)) || `HTTP ${response.status}`);
    err.status = response.status;
    err.data = data;
    throw err;
  }
  return data;
}

function esc(value) {
  return String(value).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[ch]);
}

function fmtRect(rect) {
  return `行 [${rect.row_start}, ${rect.row_end}) · 列 [${rect.col_start}, ${rect.col_end})`;
}

function showResult(el, cls, html) {
  el.hidden = false;
  el.className = `result ${cls}`;
  el.innerHTML = html;
}

/* ---------------- packages ---------------- */

async function loadPackages() {
  const data = await api("/api/packages");
  const tbody = $("#package-rows");
  $("#package-stats").textContent =
    `共 ${data.packages.length} 个包 · 最高版号 ${data.max_version} · 现行积水格 ${data.total_current_wet}`;

  if (!data.packages.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty">尚无巡查包</td></tr>';
    return;
  }

  tbody.innerHTML = data.packages.map((p) => `
    <tr>
      <td class="num">${p.version}</td>
      <td>${fmtRect(p.coverage)}</td>
      <td class="num">${p.listed_wet_points}</td>
      <td class="num">${p.current_wet_points}</td>
      <td><code>${esc(p.digest.slice(0, 12))}…</code></td>
      <td>${esc(p.imported_at)}</td>
    </tr>
  `).join("");
}

async function importPackage() {
  const btn = $("#import-btn");
  const out = $("#import-result");
  const body = $("#package-json").value.trim();
  if (!body) {
    showResult(out, "bad", "请先粘贴巡查包 JSON 或读取文件。");
    return;
  }

  let parsed;
  try {
    parsed = JSON.parse(body);
  } catch (err) {
    showResult(out, "bad", `<strong>未提交：JSON 语法错误</strong><ul><li>${esc(err.message)}</li></ul>`);
    return;
  }

  btn.disabled = true;
  try {
    const result = await api("/api/packages", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(parsed),
    });
    if (result.status === "unchanged") {
      showResult(out, "warn",
        `<span class="status-badge status-unchanged">未改变</span>
         <strong> 版号 ${result.version} 同内容重传</strong>
         <p>${esc(result.message)}</p>
         <p class="hint">摘要 <code>${esc(result.digest.slice(0, 16))}…</code> · 最高版号 ${result.max_version}</p>`);
    } else {
      const s = result.summary;
      const bits = [
        `覆盖 ${fmtRect(s.coverage)}（面积 ${s.coverage_area} 格，按矩形存储）`,
        `包内列出积水点 ${s.listed_wet_points}`,
        `新增现行积水 ${s.current_wet_added}`,
        `被本包覆盖清除 ${s.current_wet_removed}`,
      ];
      if (s.wet_points_shadowed_by_newer) {
        bits.push(`因更高版号已覆盖而保留新版结果 ${s.wet_points_shadowed_by_newer}`);
      }
      if (result.late_arrival) {
        bits.push("旧包晚到：只补新版未覆盖的区域");
      }
      const warn = result.warning
        ? `<p class="hint">注意：${esc(result.warning)}</p>`
        : "";
      showResult(out, "ok",
        `<span class="status-badge status-dry">已入仓</span>
         <strong> 版号 ${result.version}</strong>
         <ul>${bits.map((b) => `<li>${esc(b)}</li>`).join("")}</ul>${warn}`);
    }
    await refreshAll();
  } catch (err) {
    const details = (err.data && err.data.details) || [];
    const code = err.status === 409 ? "退回：版号冲突" : "退回：包校验失败";
    showResult(out, "bad",
      `<strong>${esc(code)}</strong>
       <p>${esc(err.message)}</p>
       ${details.length ? `<ul>${details.map((d) => `<li>${esc(d)}</li>`).join("")}</ul>` : ""}
       <p class="hint">整包未提交，库中覆盖范围与现行积水保持原样。</p>`);
  } finally {
    btn.disabled = false;
    await loadPackages();
  }
}

/* ---------------- box query ---------------- */

function svgEl(name, attrs) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}

function drawMap(result) {
  const svg = $("#map");
  svg.innerHTML = "";
  const box = result.box;
  const w = box.col_end - box.col_start;
  const h = box.row_end - box.row_start;
  const scale = Math.max(w, h);
  const cellSize = 100 / scale;
  const offX = (100 - w * cellSize) / 2;
  const offY = (100 - h * cellSize) / 2;

  // Surveyed background: each coverage rectangle is a single SVG rect.
  for (const rect of result.coverage_rects) {
    const r0 = Math.max(rect.row_start, box.row_start);
    const r1 = Math.min(rect.row_end, box.row_end);
    const c0 = Math.max(rect.col_start, box.col_start);
    const c1 = Math.min(rect.col_end, box.col_end);
    if (r1 <= r0 || c1 <= c0) continue;
    svg.appendChild(svgEl("rect", {
      x: offX + (c0 - box.col_start) * cellSize,
      y: offY + (r0 - box.row_start) * cellSize,
      width: (c1 - c0) * cellSize,
      height: (r1 - r0) * cellSize,
      fill: "var(--dry-fill)",
      stroke: "var(--dry-line)",
      "stroke-width": 0.15,
    }));
  }

  // Current wet cells (already limited by the API).
  const maxCells = 4000;
  result.cells.slice(0, maxCells).forEach((cell) => {
    svg.appendChild(svgEl("rect", {
      x: offX + (cell.col - box.col_start) * cellSize,
      y: offY + (cell.row - box.row_start) * cellSize,
      width: Math.max(cellSize, 0.35),
      height: Math.max(cellSize, 0.35),
      fill: "var(--wet)",
    }));
  });

  // Query frame.
  svg.appendChild(svgEl("rect", {
    x: offX, y: offY, width: w * cellSize, height: h * cellSize,
    fill: "none", stroke: "var(--accent)", "stroke-width": 0.4,
    "stroke-dasharray": "1.6 1.2",
  }));
}

async function queryBox(event) {
  if (event) event.preventDefault();
  const form = $("#box-form");
  const fd = new FormData(form);
  const params = new URLSearchParams();
  for (const key of ["row_start", "row_end", "col_start", "col_end", "limit"]) {
    params.set(key, fd.get(key));
  }
  const summary = $("#box-summary");
  const rows = $("#wet-rows");
  try {
    const result = await api(`/api/wet?${params.toString()}`);
    state.lastBox = result;
    drawMap(result);
    const area = (result.box.row_end - result.box.row_start) * (result.box.col_end - result.box.col_start);
    summary.textContent =
      `框查 ${fmtRect(result.box)}（${area} 格）· 现行积水格 ${result.count}` +
      (result.truncated ? `（已达到返回上限 ${result.limit}，结果被截断）` : "") +
      ` · 覆盖矩形 ${result.coverage_rects.length} 个`;
    if (!result.cells.length) {
      rows.innerHTML = '<tr><td colspan="3" class="empty">框内没有现行积水格</td></tr>';
    } else {
      rows.innerHTML = result.cells.map((c) =>
        `<tr><td class="num">${c.row}</td><td class="num">${c.col}</td><td class="num">${c.version}</td></tr>`
      ).join("");
    }
  } catch (err) {
    summary.textContent = `框查失败：${err.message}`;
    rows.innerHTML = `<tr><td colspan="3" class="empty">${esc(err.message)}</td></tr>`;
  }
}

/* ---------------- single cell ---------------- */

async function queryCell(event) {
  if (event) event.preventDefault();
  const fd = new FormData($("#cell-form"));
  const out = $("#cell-result");
  const params = new URLSearchParams({ row: fd.get("row"), col: fd.get("col") });
  try {
    const result = await api(`/api/cell?${params.toString()}`);
    const cls = result.status === "wet" ? "status-wet"
      : result.status === "dry" ? "status-dry" : "status-unsurveyed";
    const version = result.version == null ? "" : ` · 生效版号 ${result.version}`;
    showResult(out, result.status === "wet" ? "bad" : "ok",
      `<strong>(${result.row}, ${result.col})</strong>
       <span class="status-badge ${cls}">${esc(result.label)}</span>
       <span class="hint">${esc(version)}</span>`);
  } catch (err) {
    showResult(out, "bad", `<strong>查询失败</strong><p>${esc(err.message)}</p>`);
  }
}

/* ---------------- samples / wiring ---------------- */

async function loadSample(name) {
  try {
    const text = await (await fetch(`/static/samples/${name}`)).text();
    $("#package-json").value = text;
    $("#import-result").hidden = true;
  } catch {
    $("#package-json").value = `无法加载示例 ${name}`;
  }
}

async function resetDemo() {
  if (!confirm("清空演示库（所有巡查包与积水状态）？")) return;
  const out = $("#import-result");
  try {
    await api("/api/reset", { method: "POST" });
  } catch (err) {
    showResult(out, "bad", `<strong>未清空</strong><p>${esc(err.message)}</p>
      <p class="hint">需以 FLOOD_ALLOW_RESET=1 启动服务后才启用清空。</p>`);
    return;
  }
  out.hidden = true;
  $("#box-summary").textContent = "已清空，可重新导入示例。";
  $("#wet-rows").innerHTML = '<tr><td colspan="3" class="empty">尚未框查</td></tr>';
  $("#cell-result").hidden = true;
  $("#map").innerHTML = "";
  await refreshAll();
}

async function refreshAll() {
  await loadPackages();
}

function wire() {
  $("#import-btn").addEventListener("click", importPackage);
  $("#load-file-btn").addEventListener("click", async () => {
    const file = $("#file-input").files[0];
    if (!file) return;
    $("#package-json").value = await file.text();
  });
  document.querySelectorAll("[data-sample]").forEach((btn) => {
    btn.addEventListener("click", () => loadSample(btn.dataset.sample));
  });
  $("#reset-btn").addEventListener("click", resetDemo);
  $("#box-form").addEventListener("submit", queryBox);
  $("#cell-form").addEventListener("submit", queryCell);
  $("#package-json").addEventListener("paste", () => setTimeout(() => { $("#import-result").hidden = true; }, 0));

  loadSample("v3.json").catch(() => {});
  refreshAll().catch(() => {});
  queryBox().catch(() => {});
}

document.addEventListener("DOMContentLoaded", wire);
