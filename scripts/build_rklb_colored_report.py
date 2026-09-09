import json
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT, WD_CELL_VERTICAL_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

from build_tmc_elliott_report import add_callout, heading, page, set_cell_text, shade, table_geometry


ROOT = Path(__file__).resolve().parents[1]
DATA = json.loads((ROOT / "rklb_elliott_wave_analysis.json").read_text(encoding="utf-8"))
OUTPUT = ROOT / "RKLB_Primary1_Revised_Detailed_Color_Report_2026-07-15.docx"

INK = "17202A"
DARK = "243447"
MUTED = "65717C"
WHITE = "FFFFFF"
PALE_GREEN = "E2F0D9"
PALE_GOLD = "FFF2CC"
PALE_RED = "F4CCCC"
GREEN = "38761D"
RED = "990000"

# Named override: wave taxonomy colors. Each degree has a distinct five-wave sequence.
COLORS = {
    "P1": ("D9EAF7", "2E75B6"), "P2": ("FCE4D6", "C65911"),
    "P3": ("E2F0D9", "548235"), "P4": ("F4CCCC", "C00000"), "P5": ("E4DFEC", "7030A0"),
    "I1": ("DDEBF7", "1F4E78"), "I2": ("FFF2CC", "9C6500"),
    "I3": ("E2F0D9", "375623"), "I4": ("F4CCCC", "843C0C"), "I5": ("E4DFEC", "5B2C6F"),
    "M1": ("D9EAF7", "0070C0"), "M2": ("FFF2CC", "BF9000"),
    "M3": ("E2F0D9", "548235"), "M4": ("FCE4D6", "C65911"), "M5": ("E4DFEC", "7030A0"),
    "m1": ("CFE2F3", "0B5394"), "m2": ("FCE5CD", "B45F06"),
    "m3": ("D9EAD3", "38761D"), "m4": ("F4CCCC", "990000"), "m5": ("D9D2E9", "674EA7"),
    "A": ("F4CCCC", "990000"), "B": ("D9EAF7", "2E75B6"), "C": ("E4DFEC", "7030A0"),
}


def color_table(doc, headers, rows, widths, font_size=9.1):
    table = doc.add_table(rows=1, cols=len(headers))
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    table.autofit = False
    for index, header in enumerate(headers):
        set_cell_text(table.rows[0].cells[index], header, True, WHITE, font_size)
        shade(table.rows[0].cells[index], DARK)
        table.rows[0].cells[index].vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
    header_props = table.rows[0]._tr.get_or_add_trPr()
    repeat = OxmlElement("w:tblHeader")
    repeat.set(qn("w:val"), "true")
    header_props.append(repeat)
    for key, values in rows:
        fill, accent = COLORS[key]
        cells = table.add_row().cells
        for index, value in enumerate(values):
            set_cell_text(cells[index], str(value), index == 0, accent if index == 0 else INK, font_size)
            shade(cells[index], fill)
            cells[index].vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
    table_geometry(table, widths)
    spacer = doc.add_paragraph()
    spacer.paragraph_format.space_after = Pt(0)
    return table


def compact_legend(doc):
    table = doc.add_table(rows=1, cols=6)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    table.autofit = False
    headers = ["Degree", "Wave 1 / i", "Wave 2 / ii", "Wave 3 / iii", "Wave 4 / iv", "Wave 5 / v"]
    for index, label in enumerate(headers):
        set_cell_text(table.rows[0].cells[index], label, True, WHITE, 8.4)
        shade(table.rows[0].cells[index], DARK)
    rows = [
        ("Primary", ["P1", "P2", "P3", "P4", "P5"], ["P1", "P2", "P3", "P4", "P5"]),
        ("Intermediate", ["I1", "I2", "I3", "I4", "I5"], ["(1)", "(2)", "(3)", "(4)", "(5)"]),
        ("Minor", ["M1", "M2", "M3", "M4", "M5"], ["1", "2", "3", "4", "5"]),
        ("Minute", ["m1", "m2", "m3", "m4", "m5"], ["i", "ii", "iii", "iv", "v"]),
    ]
    for degree, keys, labels in rows:
        cells = table.add_row().cells
        set_cell_text(cells[0], degree, True, INK, 8.4)
        shade(cells[0], "F2F4F7")
        for index, (key, label) in enumerate(zip(keys, labels), 1):
            fill, accent = COLORS[key]
            set_cell_text(cells[index], label, True, accent, 8.4)
            shade(cells[index], fill)
            cells[index].vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
    table_geometry(table, [1500, 1572, 1572, 1572, 1572, 1572])
    doc.add_paragraph().paragraph_format.space_after = Pt(0)


doc = Document()
section = doc.sections[0]
section.page_width = Inches(8.5)
section.page_height = Inches(11)
section.top_margin = Inches(1)
section.bottom_margin = Inches(1)
section.left_margin = Inches(1)
section.right_margin = Inches(1)
section.header_distance = Inches(0.492)
section.footer_distance = Inches(0.492)

# standard_business_brief preset.
normal = doc.styles["Normal"]
normal.font.name = "Calibri"
normal.font.size = Pt(11)
normal.font.color.rgb = RGBColor.from_string(INK)
normal.paragraph_format.space_before = Pt(0)
normal.paragraph_format.space_after = Pt(6)
normal.paragraph_format.line_spacing = 1.10
for name, size, color, before, after in (
    ("Heading 1", 16, "2E74B5", 16, 8),
    ("Heading 2", 13, "2E74B5", 12, 6),
    ("Heading 3", 12, "1F4D78", 8, 4),
):
    style = doc.styles[name]
    style.font.name = "Calibri"
    style.font.size = Pt(size)
    style.font.bold = True
    style.font.color.rgb = RGBColor.from_string(color)
    style.paragraph_format.space_before = Pt(before)
    style.paragraph_format.space_after = Pt(after)
    style.paragraph_format.keep_with_next = True

header = section.header.paragraphs[0]
header.alignment = WD_ALIGN_PARAGRAPH.RIGHT
run = header.add_run("NASDAQ:RKLB | Elliott Wave Research")
run.font.name = "Calibri"
run.font.size = Pt(8.5)
run.font.color.rgb = RGBColor.from_string(MUTED)
footer = section.footer.paragraphs[0]
footer.alignment = WD_ALIGN_PARAGRAPH.RIGHT
field = OxmlElement("w:fldSimple")
field.set(qn("w:instr"), "PAGE")
footer._p.append(field)

# Page 1: editorial-cover opening pattern.
doc.add_paragraph().paragraph_format.space_after = Pt(28)
p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
p.paragraph_format.space_after = Pt(4)
r = p.add_run("RKLB")
r.bold = True
r.font.name = "Calibri"
r.font.size = Pt(30)
r.font.color.rgb = RGBColor.from_string(INK)
p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
p.paragraph_format.space_after = Pt(4)
r = p.add_run("Compact Multi-Degree Elliott Wave Report")
r.font.size = Pt(16)
r.font.color.rgb = RGBColor.from_string("2E74B5")
p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
p.paragraph_format.space_after = Pt(20)
r = p.add_run("Live TradingView data reviewed 15 July 2026")
r.font.size = Pt(10.5)
r.font.color.rgb = RGBColor.from_string(MUTED)

add_callout(doc, "Revised preferred count", "Primary 1 remains active. Intermediate (3) completed at $151.00; Intermediate (4) has an ABC candidate whose C leg can be counted as five waves down to $75.45 extended-hours ($75.60 regular session).", PALE_GREEN, GREEN)
add_callout(doc, "Current condition", "Price is near $79.89 premarket. The five-down C permits an Intermediate (4) low, but there is not yet a confirmed five-up reversal followed by a three-wave hold.", PALE_GOLD)
add_callout(doc, "Decision", "No confirmed trade entry. Treat $75.45-$75.60 as the key low and wait for structural confirmation.", PALE_RED, RED)
heading(doc, "Nested color rule", 2)
doc.add_paragraph("Every wave receives a distinct color within its own degree. A lower-degree wave no longer inherits the color of its parent wave.")
compact_legend(doc)

# Page 2: highest-degree count and all Intermediate waves colored separately.
page(doc, "Highest-Degree Structure")
add_callout(doc, "Degree limit", "RKLB's listed history is too short to confirm Cycle degree. Primary is the highest defensible operational degree.", "F2F4F7")
heading(doc, "Primary map", 2)
color_table(doc, ["Primary wave", "Price path", "Form", "Status"], [
    ("P1", ("Primary 1", "$3.47 -> active", "Five-wave impulse", "Intermediate (4) active")),
    ("P2", ("Primary 2", "Future", "Correction", "Not formed")),
    ("P3", ("Primary 3", "Future", "Motive advance", "Not confirmed")),
    ("P4", ("Primary 4", "Future", "Correction", "Not formed")),
    ("P5", ("Primary 5", "Future", "Final motive wave", "Not formed")),
], [1750, 2450, 2800, 2360])
heading(doc, "Inside Primary 1", 2)
color_table(doc, ["Intermediate", "Path", "Price length", "Status"], [
    ("I1", ("(1)", "$3.47 -> $33.34", "$29.87", "Complete")),
    ("I2", ("(2)", "$33.34 -> $14.71", "$18.63 retracement", "Complete")),
    ("I3", ("(3)", "$14.71 -> $151.00", "$136.29", "Complete extended impulse")),
    ("I4", ("(4)", "$151.00 -> $75.45/$75.60", "55.3% retracement", "Active / low unconfirmed")),
    ("I5", ("(5)", "Future", "Unknown", "Expected after (4)")),
], [1550, 2600, 2250, 2960])
add_callout(doc, "Revision", "The earlier completed-Primary-1 count remains legal, but its proposed Intermediate (3) did not subdivide cleanly. Treating $14.71-$151 as one extended Intermediate (3) resolves that defect.", PALE_GREEN, GREEN)

# Page 3: lower degree and explicit caveat.
page(doc, "Lower-Degree Confirmation")
heading(doc, "Inside Intermediate (1)", 2)
color_table(doc, ["Minor wave", "Price path", "Role", "Status"], [
    ("M1", ("Minor 1", "$3.47 -> $5.84", "Opening motive leg", "Probable")),
    ("M2", ("Minor 2", "$5.84 -> $4.20", "Correction", "Probable")),
    ("M3", ("Minor 3", "$4.20 -> $28.05", "Extended motive leg", "Probable")),
    ("M4", ("Minor 4", "$28.05 -> $21.87", "Correction", "Probable")),
    ("M5", ("Minor 5", "$21.87 -> $33.34", "Terminal motive leg", "Probable")),
], [1700, 2600, 2700, 2360])
add_callout(doc, "Minor result", "This candidate is price-valid: Minor 3 is not shortest and Minor 4 does not overlap Minor 1 territory.", PALE_GREEN, GREEN)
heading(doc, "Inside Extended Intermediate (3)", 2)
color_table(doc, ["Minor wave", "Price path", "Length", "Status"], [
    ("M1", ("Minor 1", "$14.71 -> $53.44", "$38.73", "Complete")),
    ("M2", ("Minor 2", "$53.44 -> $38.26", "$15.18 retracement", "Complete")),
    ("M3", ("Minor 3", "$38.26 -> $99.58", "$61.32", "Complete")),
    ("M4", ("Minor 4", "$99.58 -> $56.13", "$43.45 retracement", "Complete")),
    ("M5", ("Minor 5", "$56.13 -> $151.00", "$94.87", "Extended fifth")),
], [1700, 2700, 2200, 2760])
add_callout(doc, "Hard-rule result", "Minor 3 is not shortest. Minor 4 ended at $56.13, staying $2.69 above the Minor 1 high at $53.44. The sequence passes both hard price rules.", PALE_GREEN, GREEN)
heading(doc, "Former count", 2)
doc.add_paragraph("Primary 1 complete at $151 and Primary 2 active is retained only as the main alternate. It is downgraded because its internal Intermediate (3) created overlap and the supposed Intermediate (5) carried the strongest weekly momentum.")

# Page 4: Primary 2 and current C, each lower wave uniquely colored.
page(doc, "Intermediate (4) and Current C")
heading(doc, "Intermediate (4) corrective legs", 2)
color_table(doc, ["Leg", "Price path", "Evidence", "Status"], [
    ("A", ("A", "$151.00 -> $80.00", "Strong first decline", "Complete")),
    ("B", ("B", "$80.00 -> $107.60", "38.87% retracement of A", "Complete")),
    ("C", ("C", "$107.60 -> $75.45/$75.60", "Five-down candidate", "Complete candidate")),
], [1200, 2800, 3100, 2260])
add_callout(doc, "Why ABC is preferred", "Leg A is a sharp decline, B retraced only 38.87%, and C has a coherent five-wave internal candidate. That fits a zigzag better than a sideways Flat or W-X-Y combination, although C may still extend.", "E4DFEC", "7030A0")
heading(doc, "Inside C on the 2-hour chart", 2)
color_table(doc, ["Minute wave", "Price path", "Length", "Status"], [
    ("m1", ("i", "$107.58 -> $97.91", "$9.67", "Complete candidate")),
    ("m2", ("ii", "$97.91 -> $102.53", "$4.62 retracement", "Complete candidate")),
    ("m3", ("iii", "$102.53 -> $80.51", "$22.02", "Complete candidate")),
    ("m4", ("iv", "$80.51 -> $88.38", "$7.87 retracement", "Complete candidate")),
    ("m5", ("v", "$88.38 -> $75.45", "$12.93", "Complete candidate")),
], [1650, 2750, 2250, 2710])
add_callout(doc, "C-wave rule check", "Wave iii is not shortest. Wave iv peaked at $88.38, safely below Wave i territory ending at $97.91. The five-down count passes the hard price rules.", PALE_GREEN, GREEN)

# Page 5: indicator verification and decision levels.
page(doc, "Verification and Decision")
heading(doc, "Database checks", 2)
color_table(doc, ["Test", "Observed result", "Verdict"], [
    ("M3", ("Intermediate (3) Minor 3", "Volume $2.51B vs Minor 1 $1.53B; EWO 26.18 vs 10.51", "Passes strength check")),
    ("M4", ("Correction volume", "Minor 2 $0.63B < Minor 1; Minor 4 $1.23B < Minor 3", "Healthy volume dry-up")),
    ("M5", ("Minor 5 divergence", "Volume lower than Minor 3, but EWO 38.10 > 26.18", "No strict Volume/EWO divergence")),
    ("m3", ("C Wave iii", "Volume 5.54M > Wave i 3.08M; EWO 12.47 > 9.64", "Passes strength check")),
    ("m5", ("C Wave v exhaustion", "EWO 8.16 < Wave iii; cumulative volume 6.28M > 5.54M", "Momentum divergence only; strict test fails")),
], [2100, 4960, 2300], 8.6)
heading(doc, "Decision levels", 2)
color_table(doc, ["Level", "Meaning", "Required evidence"], [
    ("M1", ("$75.45-$75.60", "Current C/Intermediate (4) low candidate", "Must hold on the next pullback")),
    ("M2", ("$82.52", "First rebound high", "Close above and successful retest")),
    ("M3", ("$88.38", "Prior Minute iv high", "Recovery improves low confirmation")),
    ("M4", ("$92.30", "Daily resistance / prior breakdown area", "Break adds stronger reversal evidence")),
    ("M5", ("$107.60", "Intermediate (4) B high", "Break confirms a material trend reversal")),
], [1800, 3600, 3960], 8.8)
add_callout(doc, "Bullish confirmation", "A five-wave rise from $75.45-$75.60, followed by a three-wave pullback that holds above the low. Confirmation strengthens above $88.38 and $92.30.", PALE_GREEN, GREEN)
add_callout(doc, "Bearish continuation", "A sustained break below $75.45 keeps C active. $63.72 is the next 0.618 A projection. $36.60 is the A=C tail projection, not a forecast.", PALE_RED, RED)
add_callout(doc, "Structural levels", "$53.44 is internal support, not invalidation: Intermediate (4) may enter Intermediate (3)'s Minor 1 territory. The hard no-overlap boundary is $33.34, the Intermediate (1) high.", PALE_RED, RED)
add_callout(doc, "Present decision", "Wait. The C-wave structure allows a bottom, but strict volume divergence and reversal structure are not yet confirmed.", PALE_GOLD)
p = doc.add_paragraph("Technical research only. Premarket and extended-hours levels are provisional; this is not personalized investment advice.")
p.runs[0].italic = True
p.runs[0].font.size = Pt(9)
p.runs[0].font.color.rgb = RGBColor.from_string(MUTED)

doc.core_properties.title = "RKLB Compact Multi-Degree Color Elliott Wave Report"
doc.core_properties.subject = "Live TradingView Elliott Wave hierarchy with unique colors at each degree"
doc.core_properties.author = "Codex"
doc.save(OUTPUT)
print(OUTPUT)
