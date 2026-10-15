from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any


def generate_markdown_report(outputs: list[dict[str, Any]], run_report: dict[str, Any]) -> str:
    lines = [
        f"# Signalpost Company Research Batch Report",
        f"",
        f"**Run ID:** `{run_report.get('run_id')}`  ",
        f"**Started:** `{run_report.get('started_at')}`  ",
        f"**Completed:** `{run_report.get('completed_at')}`  ",
        f"**Total Inputs:** {run_report.get('input_count', len(outputs))} | **Outputs Emitted:** {len(outputs)}  ",
        f"**Requests:** {run_report.get('operations', {}).get('requests', 0)} | **Declared Cost:** ${run_report.get('operations', {}).get('third_party_cost_usd', 0.0):.2f} | **Cache Hits:** {run_report.get('operations', {}).get('cache_hits', 0)}  ",
        f"",
        f"## Operational Summary",
        f"",
        f"| Metric | Value |",
        f"|---|---|",
        f"| Emitted Envelopes | {len(outputs)} |",
        f"| Unique Organisations | {run_report.get('unique_output_count', len(outputs))} |",
        f"| Terminal Status | {json.dumps(run_report.get('terminal_status_counts', {}))} |",
        f"| Total Outbound Requests | {run_report.get('operations', {}).get('requests', 0)} |",
        f"| Latency p50 | {run_report.get('operations', {}).get('p50_latency_ms', 'N/A')} ms |",
        f"| Latency p95 | {run_report.get('operations', {}).get('p95_latency_ms', 'N/A')} ms |",
        f"| Qualification Status | {'Qualified' if run_report.get('qualification_passed') else 'Smoke/Local Validation'} |",
        f"",
        f"## Researched Companies",
        f"",
    ]

    for item in outputs[:100]:
        org = item.get("organisation_number", "Unknown")
        anchor = item.get("identity_anchor") or {}
        name = anchor.get("official_name") or f"Org {org}"
        summary = item.get("summary") or "No summary available."
        claims = item.get("claims", [])
        avail_counts = {}
        for c in claims:
            avail_counts[c.get("availability", "unknown")] = avail_counts.get(c.get("availability", "unknown"), 0) + 1

        lines.extend([
            f"### {name} (`{org}`)",
            f"",
            f"**Terminal Status:** `{item.get('run', {}).get('terminal_status', 'completed')}` | **Claims Found:** {len(claims)} (Available: {avail_counts.get('available', 0)}, Ambiguous: {avail_counts.get('ambiguous', 0)}, Not Available: {avail_counts.get('not_available', 0)})",
            f"",
            f"> {summary}",
            f"",
        ])

        # Key claims
        key_claims = [c for c in claims if c.get("availability") == "available" and c.get("field") in {"legal_form", "official_website", "employees", "registered_roles", "annual_accounts"} or c.get("field", "").startswith("financial.")]
        if key_claims:
            lines.append("| Field | Value | Confidence | Evidence IDs |")
            lines.append("|---|---|---|---|")
            for kc in key_claims[:10]:
                val = kc.get("value")
                val_str = json.dumps(val, ensure_ascii=False) if isinstance(val, (dict, list)) else str(val)
                val_str = (val_str[:60] + "...") if len(val_str) > 60 else val_str
                lines.append(f"| `{kc.get('field')}` | {val_str} | {kc.get('confidence', 1.0)} | `{', '.join(kc.get('evidence_ids', []))}` |")
            lines.append("")

    if len(outputs) > 100:
        lines.append(f"*Note: Showing first 100 of {len(outputs)} companies in Markdown report.*")
        lines.append("")

    return "\n".join(lines)


def generate_html_report(outputs: list[dict[str, Any]], run_report: dict[str, Any]) -> str:
    ops = run_report.get("operations", {})
    term_counts = run_report.get("terminal_status_counts", {})
    term_counts_str = ", ".join(f"{k}: {v}" for k, v in term_counts.items()) or "None"

    cards_html = []
    for idx, item in enumerate(outputs):
        org = item.get("organisation_number", "Unknown")
        anchor = item.get("identity_anchor") or {}
        name = anchor.get("official_name") or f"Company {org}"
        summary = item.get("summary") or "No summary recorded."
        claims = item.get("claims", [])
        evidence_list = item.get("evidence", [])
        changes = item.get("changes", [])
        errors = item.get("errors", [])
        item_ops = item.get("operations", {})
        term_status = item.get("run", {}).get("terminal_status", "completed")

        avail_pills = []
        counts: dict[str, int] = {}
        for c in claims:
            st = c.get("availability", "unknown")
            counts[st] = counts.get(st, 0) + 1
        for st, cnt in sorted(counts.items()):
            color = {"available": "#10b981", "not_available": "#64748b", "blocked": "#f59e0b",
                     "not_applicable": "#94a3b8", "ambiguous": "#ef4444", "failed": "#dc2626"}.get(st, "#6b7280")
            avail_pills.append(f'<span class="pill" style="background:{color}20; color:{color}; border:1px solid {color}40">{st}: {cnt}</span>')

        claims_rows = []
        for c in claims:
            fld = c.get("field", "")
            val = c.get("value")
            val_disp = json.dumps(val, ensure_ascii=False) if isinstance(val, (dict, list)) else str(val or "")
            if len(val_disp) > 120:
                val_disp = val_disp[:120] + "..."
            av = c.get("availability", "available")
            conf = c.get("confidence", 1.0)
            eids = ", ".join(c.get("evidence_ids", []))
            claims_rows.append(f"""
            <tr>
              <td><code>{html.escape(fld)}</code></td>
              <td><span class="status-{av}">{av}</span></td>
              <td>{html.escape(val_disp)}</td>
              <td>{conf:.2f}</td>
              <td><small>{html.escape(eids)}</small></td>
            </tr>
            """)

        cards_html.append(f"""
        <div class="company-card" id="comp-{html.escape(org)}">
          <div class="card-header">
            <div>
              <h3>{html.escape(name)} <span class="orgnr">#{html.escape(org)}</span></h3>
              <div class="pills-row">{' '.join(avail_pills)}</div>
            </div>
            <div class="card-meta">
              <span class="badge badge-{term_status}">{term_status}</span>
              <small>Reqs: {item_ops.get('requests', 0)} | {item_ops.get('runtime_ms', 0)}ms</small>
            </div>
          </div>
          <div class="card-summary">
            <strong>Evidence-Grounded Summary:</strong>
            <p>{html.escape(summary)}</p>
          </div>
          {f'<div class="changes-box"><strong>Detected Changes:</strong> {len(changes)} field(s) updated vs previous snapshot.</div>' if changes else ''}
          {f'<div class="errors-box"><strong>Errors/Warnings:</strong> {len(errors)} recorded.</div>' if errors else ''}
          <details>
            <summary>View {len(claims)} Fact Claims & {len(evidence_list)} Evidence Items</summary>
            <div class="table-wrap">
              <table class="claims-table">
                <thead>
                  <tr><th>Field</th><th>Status</th><th>Value</th><th>Conf</th><th>Evidence</th></tr>
                </thead>
                <tbody>
                  {''.join(claims_rows)}
                </tbody>
              </table>
            </div>
          </details>
        </div>
        """)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Signalpost Company Research Report — {html.escape(run_report.get('run_id', ''))}</title>
  <style>
    :root {{
      --bg: #0f172a;
      --card-bg: #1e293b;
      --border: #334155;
      --text: #f8fafc;
      --muted: #94a3b8;
      --accent: #38bdf8;
      --success: #10b981;
      --warning: #f59e0b;
      --danger: #ef4444;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
      padding: 2rem;
    }}
    .container {{ max-width: 1200px; mx: auto; margin: 0 auto; }}
    header {{
      border-b: 1px solid var(--border);
      padding-bottom: 1.5rem;
      margin-bottom: 2rem;
      border-bottom: 1px solid var(--border);
    }}
    h1 {{ font-size: 1.8rem; margin-bottom: 0.5rem; }}
    .stats-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 1rem;
      margin: 1.5rem 0;
    }}
    .stat-box {{
      background: var(--card-bg);
      padding: 1rem;
      border-radius: 8px;
      border: 1px solid var(--border);
    }}
    .stat-label {{ font-size: 0.75rem; text-transform: uppercase; color: var(--muted); }}
    .stat-value {{ font-size: 1.5rem; font-weight: bold; color: var(--accent); margin-top: 0.25rem; }}
    .company-card {{
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 8px;
      margin-bottom: 1.25rem;
      padding: 1.25rem;
    }}
    .card-header {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      margin-bottom: 0.75rem;
    }}
    .card-header h3 {{ font-size: 1.2rem; }}
    .orgnr {{ font-size: 0.85rem; color: var(--muted); font-family: monospace; }}
    .pills-row {{ display: flex; flex-wrap: wrap; gap: 0.4rem; margin-top: 0.5rem; }}
    .pill {{ font-size: 0.7rem; padding: 0.15rem 0.5rem; border-radius: 4px; font-weight: 500; }}
    .badge {{ padding: 0.2rem 0.6rem; border-radius: 4px; font-size: 0.75rem; font-weight: bold; }}
    .badge-completed {{ background: #10b98120; color: #10b981; border: 1px solid #10b981; }}
    .badge-failed {{ background: #ef444420; color: #ef4444; border: 1px solid #ef4444; }}
    .card-summary {{ background: #0f172a60; padding: 0.75rem; border-radius: 6px; font-size: 0.9rem; margin-bottom: 0.75rem; }}
    .changes-box {{ background: #38bdf815; border-left: 3px solid var(--accent); padding: 0.5rem 0.75rem; font-size: 0.85rem; margin-bottom: 0.5rem; }}
    .errors-box {{ background: #ef444415; border-left: 3px solid var(--danger); padding: 0.5rem 0.75rem; font-size: 0.85rem; margin-bottom: 0.5rem; }}
    details {{ margin-top: 0.75rem; }}
    summary {{ cursor: pointer; color: var(--accent); font-size: 0.85rem; font-weight: 500; }}
    .table-wrap {{ overflow-x: auto; margin-top: 0.5rem; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.8rem; }}
    th, td {{ padding: 0.4rem 0.6rem; border: 1px solid var(--border); text-align: left; }}
    th {{ background: #0f172a; color: var(--muted); }}
    .status-available {{ color: #10b981; font-weight: 500; }}
    .status-not_available {{ color: #94a3b8; }}
    .status-blocked {{ color: #f59e0b; }}
    .status-ambiguous {{ color: #ef4444; }}
    .status-failed {{ color: #dc2626; }}
  </style>
</head>
<body>
  <div class="container">
    <header>
      <h1>Signalpost Research Report</h1>
      <p style="color:var(--muted)">Run: <code>{html.escape(run_report.get('run_id', ''))}</code> | Finished: {html.escape(run_report.get('completed_at', ''))}</p>
      <div class="stats-grid">
        <div class="stat-box">
          <div class="stat-label">Companies Processed</div>
          <div class="stat-value">{len(outputs)}</div>
        </div>
        <div class="stat-box">
          <div class="stat-label">Outbound Requests</div>
          <div class="stat-value">{ops.get('requests', 0)}</div>
        </div>
        <div class="stat-box">
          <div class="stat-label">Cache Hits</div>
          <div class="stat-value">{ops.get('cache_hits', 0)}</div>
        </div>
        <div class="stat-box">
          <div class="stat-label">Declared Spend</div>
          <div class="stat-value">${ops.get('third_party_cost_usd', 0.0):.2f}</div>
        </div>
        <div class="stat-box">
          <div class="stat-label">Latency p50 / p95</div>
          <div class="stat-value" style="font-size:1.1rem">{ops.get('p50_latency_ms', 0)} / {ops.get('p95_latency_ms', 0)} ms</div>
        </div>
      </div>
    </header>
    <main>
      {''.join(cards_html)}
    </main>
  </div>
</body>
</html>
"""
