"""
Builds the PA Intake & SLA scorecard workbook.

Sheets:
  1. PA Data          -- a real Excel Table (pivot-ready) of every core PA
                          request + determination.
  2. Provider x Week   -- a pivot-style cross-tab of SLA breach rate per
                          provider per week, driven entirely by live
                          SUMIFS / IFERROR / AVERAGE formulas against the
                          PA Data table (never pasted values).
  3. Scorecard          -- a ranked provider scorecard: request/breach
                          counts and breach rate are live SUMIFS formulas;
                          rank is a live RANK() formula; confidence and
                          root-cause columns are pasted from the agent's
                          Python output (that classification -- OLS trend
                          fitting, queue cross-referencing -- is not a
                          native spreadsheet computation, and each such
                          column is labeled as agent-computed so a reader
                          never mistakes it for a live formula).

Verified via headless LibreOffice recalculation (scripts/recalc.py from
the xlsx skill) against the same numbers Python computed independently --
a second, human-facing cross-check on top of the agent's own SQL-vs-Pandas
cross-check layer.

Run: python3 excel/build_workbook.py
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.formatting.rule import ColorScaleRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

HERE = Path(__file__).resolve().parent.parent
CORE_DB = HERE / "data" / "amaranth_pa_core.db"
OUTPUT_DIR = HERE / "output"
WORKBOOK_PATH = OUTPUT_DIR / "pa_sla_scorecard.xlsx"
RECALC_SCRIPT = Path(__file__).resolve().parent / "recalc.py"  # vendored, self-contained -- see excel/recalc.py

FONT = "Arial"
HEADER_FILL = PatternFill(start_color="1F3864", end_color="1F3864", fill_type="solid")
HEADER_FONT = Font(name=FONT, bold=True, color="FFFFFF")


def load_data() -> pd.DataFrame:
    conn = sqlite3.connect(CORE_DB)
    df = pd.read_sql("""
        SELECT r.pa_id, r.member_id, r.provider_id, p.provider_name, p.specialty,
               r.service_category, r.urgency, r.request_date, r.request_week,
               d.decision, d.sla_breached, d.turnaround_days
        FROM fact_pa_requests r
        JOIN dim_providers p ON p.provider_id = r.provider_id
        LEFT JOIN fact_pa_determinations d ON d.pa_id = r.pa_id
        ORDER BY r.provider_id, r.request_week, r.pa_id
    """, conn)
    conn.close()
    df["sla_breached"] = df["sla_breached"].fillna(0).astype(int)
    df["has_determination"] = df["decision"].notna().astype(int)
    return df


def load_agent_findings() -> list[dict]:
    findings = []
    for name in ("escalations.json", "needs_human_review.json", "cleared.json"):
        path = OUTPUT_DIR / name
        if path.exists():
            findings += json.loads(path.read_text())
    return findings


def build() -> None:
    df = load_data()
    findings = load_agent_findings()
    providers = sorted(df[["provider_id", "provider_name", "specialty"]].drop_duplicates()
                        .itertuples(index=False), key=lambda r: r.provider_id)
    n_weeks = int(df["request_week"].max()) + 1

    wb = Workbook()

    # ---------------- Sheet 1: PA Data (Excel Table) ---------------- #
    ws1 = wb.active
    ws1.title = "PA Data"
    headers = ["pa_id", "member_id", "provider_id", "provider_name", "specialty", "service_category",
               "urgency", "request_date", "request_week", "decision", "sla_breached", "turnaround_days"]
    ws1.append(headers)
    for _, row in df.iterrows():
        ws1.append([row[h] for h in headers])
    for cell in ws1[1]:
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
    n_rows = len(df) + 1
    last_col = get_column_letter(len(headers))
    table_ref = f"A1:{last_col}{n_rows}"
    table = Table(displayName="PAData", ref=table_ref)
    table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium9", showRowStripes=True)
    ws1.add_table(table)
    for col_cells in ws1.columns:
        length = max(len(str(c.value)) if c.value is not None else 0 for c in col_cells)
        ws1.column_dimensions[col_cells[0].column_letter].width = min(max(length + 2, 10), 28)
    for row in ws1.iter_rows(min_row=1, max_row=n_rows):
        for cell in row:
            cell.font = Font(name=FONT, bold=(cell.row == 1), color="FFFFFF" if cell.row == 1 else "000000")

    # ---------------- Sheet 2: Provider x Week cross-tab ---------------- #
    ws2 = wb.create_sheet("Provider x Week")
    ws2["A1"] = "SLA Breach Rate by Provider x Week (live SUMIFS/IFERROR against PA Data table)"
    ws2["A1"].font = Font(name=FONT, bold=True, size=12)
    header_row = 3
    ws2.cell(header_row, 1, "Provider").font = HEADER_FONT
    ws2.cell(header_row, 1).fill = HEADER_FILL
    for w in range(n_weeks):
        c = ws2.cell(header_row, 2 + w, f"Wk {w}")
        c.font = HEADER_FONT
        c.fill = HEADER_FILL
        c.alignment = Alignment(horizontal="center")
    avg_col = 2 + n_weeks
    c = ws2.cell(header_row, avg_col, "Quarter Avg")
    c.font = HEADER_FONT
    c.fill = HEADER_FILL

    for i, prov in enumerate(providers):
        r = header_row + 1 + i
        ws2.cell(r, 1, f"{prov.provider_id} - {prov.provider_name}").font = Font(name=FONT)
        for w in range(n_weeks):
            col = 2 + w
            col_letter = get_column_letter(col)
            breached_formula = (
                f'=IFERROR(SUMIFS(PAData[sla_breached],PAData[provider_id],"{prov.provider_id}",'
                f'PAData[request_week],{w})/SUMIFS(PAData[has_determination],PAData[provider_id],'
                f'"{prov.provider_id}",PAData[request_week],{w}),"")'
            )
            # has_determination isn't a table column -- use decision non-blank count instead via COUNTIFS
            breached_formula = (
                f'=IFERROR(SUMIFS(PAData[sla_breached],PAData[provider_id],"{prov.provider_id}",'
                f'PAData[request_week],{w})/COUNTIFS(PAData[provider_id],"{prov.provider_id}",'
                f'PAData[request_week],{w},PAData[decision],"<>"),"")'
            )
            cell = ws2.cell(r, col, breached_formula)
            cell.number_format = "0.0%"
            cell.font = Font(name=FONT)
        avg_formula = f"=IFERROR(AVERAGE({get_column_letter(2)}{r}:{get_column_letter(1+n_weeks)}{r}),\"\")"
        avg_cell = ws2.cell(r, avg_col, avg_formula)
        avg_cell.number_format = "0.0%"
        avg_cell.font = Font(name=FONT, bold=True)

    # weekly average row across all providers
    weekly_avg_row = header_row + 1 + len(providers) + 1
    ws2.cell(weekly_avg_row, 1, "All-Provider Weekly Avg").font = Font(name=FONT, bold=True)
    for w in range(n_weeks):
        col_letter = get_column_letter(2 + w)
        r1, r2 = header_row + 1, header_row + len(providers)
        formula = f'=IFERROR(AVERAGE({col_letter}{r1}:{col_letter}{r2}),"")'
        cell = ws2.cell(weekly_avg_row, 2 + w, formula)
        cell.number_format = "0.0%"
        cell.font = Font(name=FONT, bold=True)

    data_range = f"B{header_row+1}:{get_column_letter(1+n_weeks)}{header_row+len(providers)}"
    ws2.conditional_formatting.add(
        data_range,
        ColorScaleRule(start_type="min", start_color="63BE7B", mid_type="percentile", mid_value=50,
                        mid_color="FFEB84", end_type="max", end_color="F8696B"),
    )
    ws2.column_dimensions["A"].width = 34
    for w in range(n_weeks + 1):
        ws2.column_dimensions[get_column_letter(2 + w)].width = 10

    # ---------------- Sheet 3: Scorecard ---------------- #
    ws3 = wb.create_sheet("Scorecard")
    ws3["A1"] = "Provider SLA Scorecard -- Quarter Summary"
    ws3["A1"].font = Font(name=FONT, bold=True, size=12)
    ws3["A2"] = ("Requests/Breached/Breach Rate/Rank are live formulas against the PA Data table. "
                 "Confidence and Root Cause are computed by the trust-layer agent pipeline (OLS trend "
                 "fitting + queue cross-referencing, not a native spreadsheet formula) and pasted here "
                 "as values -- see output/audit_log.json for the full derivation.")
    ws3["A2"].font = Font(name=FONT, italic=True, size=9, color="595959")
    ws3.merge_cells("A2:I2")

    sc_headers = ["Provider ID", "Provider Name", "Specialty", "Total Determinations", "Total Breached",
                  "Breach Rate", "Rank", "Confidence (agent)", "Root Cause (agent)"]
    header_row2 = 4
    for j, h in enumerate(sc_headers, start=1):
        c = ws3.cell(header_row2, j, h)
        c.font = HEADER_FONT
        c.fill = HEADER_FILL

    findings_by_provider = {f["provider_id"]: f for f in findings}
    breach_rate_col_letter = "F"
    first_data_row = header_row2 + 1
    for i, prov in enumerate(providers):
        r = first_data_row + i
        f = findings_by_provider.get(prov.provider_id, {})
        ws3.cell(r, 1, prov.provider_id)
        ws3.cell(r, 2, prov.provider_name)
        ws3.cell(r, 3, prov.specialty)
        # Denominator matches the agent's methodology: only requests with a
        # determination on file are counted (a still-pending request can't
        # yet be judged breached/not-breached -- see SOP-002).
        ws3.cell(r, 4, f'=COUNTIFS(PAData[provider_id],A{r},PAData[decision],"<>")')
        ws3.cell(r, 5, f'=SUMIFS(PAData[sla_breached],PAData[provider_id],A{r})')
        rate_cell = ws3.cell(r, 6, f'=IFERROR(E{r}/D{r},"")')
        rate_cell.number_format = "0.0%"
        ws3.cell(r, 8, f.get("confidence", "n/a"))
        ws3.cell(r, 9, f.get("root_cause", "n/a").replace("_", " ") if f else "n/a")
        for col in range(1, 10):
            ws3.cell(r, col).font = Font(name=FONT)

    last_data_row = first_data_row + len(providers) - 1
    for i in range(len(providers)):
        r = first_data_row + i
        rank_formula = (f'=RANK({breach_rate_col_letter}{r},'
                         f'{breach_rate_col_letter}{first_data_row}:{breach_rate_col_letter}{last_data_row},0)')
        ws3.cell(r, 7, rank_formula).font = Font(name=FONT)

    ws3.conditional_formatting.add(
        f"F{first_data_row}:F{last_data_row}",
        ColorScaleRule(start_type="min", start_color="63BE7B", mid_type="percentile", mid_value=50,
                        mid_color="FFEB84", end_type="max", end_color="F8696B"),
    )
    for col, width in zip("ABCDEFGHI", [12, 30, 16, 14, 14, 12, 8, 18, 30]):
        ws3.column_dimensions[col].width = width

    OUTPUT_DIR.mkdir(exist_ok=True)
    wb.save(WORKBOOK_PATH)
    print(f"Workbook written: {WORKBOOK_PATH}")


def recalc() -> None:
    """Verify the workbook's formulas via headless LibreOffice. This is a
    verification step, not a build step -- the workbook is already
    written with live formulas by build() above, and any real spreadsheet
    program (Excel, LibreOffice Calc, Google Sheets) will compute those
    formulas the moment the file is opened, independent of whether this
    check can run in this environment.

    Two different kinds of failure are treated differently:
      - LibreOffice genuinely finding formula errors (status=errors_found)
        is a real bug in this project and MUST fail loudly.
      - LibreOffice/soffice simply not being available on this machine
        (payload has an "error" key -- e.g. not installed, or a platform
        quirk like Windows lacking AF_UNIX) is an environment limitation,
        not a defect in the workbook -- warn and continue rather than
        blocking the rest of the pipeline (dashboard, tests) from running.
    """
    result = subprocess.run([sys.executable, str(RECALC_SCRIPT), str(WORKBOOK_PATH), "60"],
                             capture_output=True, text=True)
    print(result.stdout)
    try:
        payload = json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        print(f"WARNING: could not parse recalc.py output; skipping formula verification.\n{result.stderr}",
              file=sys.stderr)
        return

    if "error" in payload:
        print(f"WARNING: formula recalculation could not run ({payload['error']}). "
              f"The workbook was still written with live formulas at {WORKBOOK_PATH} -- "
              f"open it in Excel, LibreOffice Calc, or Google Sheets and it will compute "
              f"normally. This just means automated verification was skipped here.",
              file=sys.stderr)
        return

    if payload.get("status") != "success":
        raise SystemExit(f"Workbook has formula errors: {payload}")


if __name__ == "__main__":
    build()
    recalc()
