"""PDF reports (ReportLab): per-student proctoring report + exam summary."""
import io
from xml.sax.saxutils import escape

from PIL import Image as PILImage
from reportlab.graphics.shapes import Drawing, Rect, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (Image, PageBreak, Paragraph, SimpleDocTemplate,
                                Spacer, Table, TableStyle)

import scoring
from utils import fmt_duration, fmt_utc

INDIGO = colors.HexColor("#4f46e5")
SLATE = colors.HexColor("#334155")
LIGHT = colors.HexColor("#f1f5f9")
RISK_COLORS = {"Low": colors.HexColor("#16a34a"), "Medium": colors.HexColor("#d97706"),
               "High": colors.HexColor("#dc2626")}
MAX_EVIDENCE_IMAGES = 30

_ss = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=_ss["Title"], fontSize=20, textColor=INDIGO, alignment=0, spaceAfter=4)
H2 = ParagraphStyle("H2", parent=_ss["Heading2"], fontSize=13, textColor=SLATE, spaceBefore=14, spaceAfter=6)
BODY = ParagraphStyle("B", parent=_ss["BodyText"], fontSize=9.5, leading=13)
SMALL = ParagraphStyle("S", parent=BODY, fontSize=8, textColor=colors.HexColor("#64748b"), leading=10)
CELL = ParagraphStyle("C", parent=BODY, fontSize=8.5, leading=11)


def P(text, style=BODY):
    return Paragraph(escape(str(text)), style)


def _footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(colors.HexColor("#94a3b8"))
    canvas.drawString(1.8 * cm, 1.1 * cm, "AI Proctor - confidential. AI flags are indicators for human review, not proof of misconduct.")
    canvas.drawRightString(A4[0] - 1.8 * cm, 1.1 * cm, f"Page {doc.page}")
    canvas.restoreState()


def _table(data, widths, header=True, extra=None):
    t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
    style = [
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    if header:
        style += [("BACKGROUND", (0, 0), (-1, 0), INDIGO), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                  ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                  ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT])]
    t.setStyle(TableStyle(style + (extra or [])))
    return t


def _bar_chart(rows, width=16.4 * cm):
    """Horizontal bars: incidents per violation type."""
    if not rows:
        return None
    row_h, label_w = 16, 150
    h = row_h * len(rows) + 8
    d = Drawing(width, h)
    mx = max(r["count"] for r in rows) or 1
    bar_max = width - label_w - 40
    for i, r in enumerate(rows):
        y = h - (i + 1) * row_h
        d.add(String(0, y + 4, r["label"][:32], fontSize=8, fillColor=SLATE))
        d.add(Rect(label_w, y + 2, max(3, bar_max * r["count"] / mx), 10,
                   fillColor=colors.HexColor(r["color"]), strokeColor=None))
        d.add(String(label_w + max(3, bar_max * r["count"] / mx) + 5, y + 4, str(r["count"]),
                     fontSize=8, fillColor=SLATE))
    return d


def _timeline_chart(buckets, width=16.4 * cm, height=60):
    d = Drawing(width, height + 14)
    mx = max(buckets) or 1
    n = len(buckets)
    bw = (width - 10) / n
    for i, v in enumerate(buckets):
        bh = (height - 10) * v / mx if v else 1
        d.add(Rect(5 + i * bw + 2, 12, bw - 4, bh, fillColor=INDIGO if v else colors.HexColor("#cbd5e1"), strokeColor=None))
        if v:
            d.add(String(5 + i * bw + bw / 2 - 2, 14 + bh, str(v), fontSize=7, fillColor=SLATE))
    d.add(String(5, 0, "Start of exam", fontSize=7, fillColor=colors.HexColor("#64748b")))
    d.add(String(width - 60, 0, "End of exam", fontSize=7, fillColor=colors.HexColor("#64748b")))
    return d


def _thumb(blob, max_w=7.6 * cm):
    """Re-encode a stored screenshot as a small JPEG and return a flowable."""
    try:
        im = PILImage.open(io.BytesIO(blob)).convert("RGB")
        im.thumbnail((560, 420))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=70)
        buf.seek(0)
        ratio = im.height / im.width
        return Image(buf, width=max_w, height=max_w * ratio)
    except Exception:
        return P("(image unavailable)", SMALL)


def build_attempt_pdf(ctx):
    """
    ctx keys: exam, student, attempt, total_marks, answers[list], violations[list incl. screenshot],
              breakdown, analysis, buckets, duration_sec
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=1.8 * cm, rightMargin=1.8 * cm,
                            topMargin=1.6 * cm, bottomMargin=1.8 * cm,
                            title=f"Proctoring report - {ctx['student']['name']}")
    att, exam, st = ctx["attempt"], ctx["exam"], ctx["student"]
    pct_score = (att["score"] / ctx["total_marks"] * 100) if ctx["total_marks"] else 0
    level = att["risk_level"]
    story = [P("AI Proctored Examination Report", H1),
             P(f"{exam['title']}", ParagraphStyle("sub", parent=BODY, fontSize=11, textColor=SLATE)),
             Spacer(1, 8)]

    info = [
        [P("Student", CELL), P(st["name"], CELL), P("Email", CELL), P(st["email"], CELL)],
        [P("Started", CELL), P(fmt_utc(att["started_at"]), CELL), P("Submitted", CELL), P(fmt_utc(att["submitted_at"]), CELL)],
        [P("Time used", CELL), P(fmt_duration(ctx["duration_sec"]), CELL), P("Status", CELL),
         P(att["status"].replace("_", " ").title(), CELL)],
    ]
    story.append(_table(info, [2.6 * cm, 6.2 * cm, 2.6 * cm, 6 * cm], header=False,
                        extra=[("BACKGROUND", (0, 0), (0, -1), LIGHT), ("BACKGROUND", (2, 0), (2, -1), LIGHT)]))
    story.append(Spacer(1, 10))

    big = ParagraphStyle("big", parent=BODY, fontSize=18, leading=22, alignment=1)
    cap = ParagraphStyle("cap", parent=SMALL, alignment=1)
    cards = Table([[
        [Paragraph(f"<b>{att['score']:g} / {ctx['total_marks']:g}</b>", big), Paragraph(f"Score ({pct_score:.0f}%)", cap)],
        [Paragraph(f"<font color='{RISK_COLORS[level].hexval().replace('0x', '#')}'><b>{att['misuse_pct']:g}%</b></font>", big),
         Paragraph("Cheating / misuse likelihood", cap)],
        [Paragraph(f"<font color='{RISK_COLORS[level].hexval().replace('0x', '#')}'><b>{level}</b></font>", big),
         Paragraph("Risk level", cap)],
        [Paragraph(f"<b>{att['violation_count']}</b>", big), Paragraph("Violations recorded", cap)],
    ]], colWidths=[4.1 * cm] * 4)
    cards.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#cbd5e1")),
                               ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#e2e8f0")),
                               ("BACKGROUND", (0, 0), (-1, -1), LIGHT),
                               ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    story.append(cards)
    if att["pending"]:
        story.append(Spacer(1, 4))
        story.append(P(f"{att['pending']} descriptive answer(s) are still awaiting manual grading; the score may change.", SMALL))

    # ---- analysis
    story.append(P("Analysis", H2))
    for note in ctx["analysis"]:
        story.append(Paragraph("&bull; " + escape(note), BODY))
        story.append(Spacer(1, 2))

    # ---- breakdown
    story.append(P("Violation breakdown", H2))
    if ctx["breakdown"]:
        chart = _bar_chart(ctx["breakdown"])
        story.append(chart)
        story.append(Spacer(1, 6))
        rows = [["Violation type", "Incidents", "Weight", "Counted (cap)", "Risk points"]]
        for r in ctx["breakdown"]:
            rows.append([P(r["label"], CELL), r["count"], r["weight"], f"{min(r['count'], r['cap'])} / {r['cap']}", f"{r['points']:g}"])
        story.append(_table(rows, [6.6 * cm, 2.2 * cm, 2.2 * cm, 3 * cm, 2.4 * cm]))
        story.append(Spacer(1, 6))
        story.append(P("Activity over the exam duration (incidents per tenth of the exam)", SMALL))
        story.append(_timeline_chart(ctx["buckets"]))
    else:
        story.append(P("No violations were recorded."))
    story.append(P("Formula: points = sum(weight x min(count, cap)); misuse % = 100 x (1 - e^(-points/60)). "
                   "Low < 25%, Medium 25-50%, High >= 50%.", SMALL))

    # ---- performance
    story.append(P("Student performance", H2))
    rows = [["#", "Question", "Type", "Answer", "Marks"]]
    for i, a in enumerate(ctx["answers"], 1):
        ans = a["answer"].strip() or "(no answer)"
        if a["qtype"] == "mcq" and a["answer"].strip().isdigit() and int(a["answer"]) < len(a["options"]):
            ans = a["options"][int(a["answer"])]
        marks = a["manual_marks"] if a["manual_marks"] is not None else a["auto_marks"]
        mtxt = "pending" if marks is None else f"{marks:g} / {a['marks']:g}"
        rows.append([i, P(a["text"][:220], CELL), a["qtype"].upper(), P(ans[:220], CELL), mtxt])
    story.append(_table(rows, [0.8 * cm, 6.4 * cm, 1.8 * cm, 5.6 * cm, 2.4 * cm]))

    # ---- evidence
    story.append(PageBreak())
    story.append(P("Violation log & evidence", H2))
    vios = ctx["violations"]
    if not vios:
        story.append(P("No evidence to display."))
    else:
        rows = [["Time (UTC)", "T+", "Violation", "Detail"]]
        for v in vios:
            mm, ss = divmod(v["offset_sec"], 60)
            rows.append([fmt_utc(v["occurred_at"])[-12:], f"{mm:02d}:{ss:02d}", P(scoring.label(v["vtype"]), CELL), P(v["detail"], CELL)])
        story.append(_table(rows, [3.0 * cm, 1.6 * cm, 5.6 * cm, 6.8 * cm]))
        with_img = [v for v in vios if v.get("screenshot")][:MAX_EVIDENCE_IMAGES]
        if with_img:
            story.append(P("Screenshots", H2))
            cells = []
            for v in with_img:
                mm, ss = divmod(v["offset_sec"], 60)
                cap_txt = f"{scoring.label(v['vtype'])} - T+{mm:02d}:{ss:02d}"
                cells.append([_thumb(v["screenshot"]), Spacer(1, 2), P(cap_txt, SMALL)])
            if len([x for x in vios if x.get("screenshot")]) > MAX_EVIDENCE_IMAGES:
                story.append(P(f"Showing the first {MAX_EVIDENCE_IMAGES} screenshots; view the rest in the dashboard.", SMALL))
            grid = []
            for i in range(0, len(cells), 2):
                pair = cells[i:i + 2]
                if len(pair) == 1:
                    pair.append("")
                grid.append(pair)
            t = Table(grid, colWidths=[8.6 * cm, 8.6 * cm])
            t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
            story.append(t)
    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()


def build_exam_pdf(ctx):
    """ctx: exam, total_marks, rows[list], stats{}, violation_totals[list]"""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=1.8 * cm, rightMargin=1.8 * cm,
                            topMargin=1.6 * cm, bottomMargin=1.8 * cm, title=f"Exam summary - {ctx['exam']['title']}")
    ex, s = ctx["exam"], ctx["stats"]
    story = [P("Exam Summary Report", H1), P(ex["title"], ParagraphStyle("sub", parent=BODY, fontSize=11, textColor=SLATE)),
             P(f"Window: {fmt_utc(ex['start_at'])} - {fmt_utc(ex['end_at'])}   |   Duration: {ex['duration_min']} min", SMALL),
             Spacer(1, 10)]
    summary = [
        ["Assigned students", s["assigned"], "Completed", s["completed"]],
        ["Average score", f"{s['avg_score_pct']:.1f}%", "Average misuse likelihood", f"{s['avg_misuse']:.1f}%"],
        ["High-risk attempts", s["high"], "Medium-risk attempts", s["medium"]],
    ]
    story.append(_table(summary, [4.4 * cm, 3.6 * cm, 5.4 * cm, 3.8 * cm], header=False,
                        extra=[("BACKGROUND", (0, 0), (0, -1), LIGHT), ("BACKGROUND", (2, 0), (2, -1), LIGHT)]))
    story.append(P("Results by student", H2))
    rows = [["Student", "Status", "Score", "Score %", "Misuse %", "Risk", "Violations"]]
    extra = []
    for i, r in enumerate(ctx["rows"], 1):
        pct = (r["score"] / ctx["total_marks"] * 100) if ctx["total_marks"] else 0
        rows.append([P(r["name"], CELL), r["status"].replace("_", " ").title(), f"{r['score']:g}/{ctx['total_marks']:g}",
                     f"{pct:.0f}%", f"{r['misuse_pct']:g}%", r["risk_level"], r["violation_count"]])
        extra.append(("TEXTCOLOR", (5, i), (5, i), RISK_COLORS.get(r["risk_level"], SLATE)))
    story.append(_table(rows, [4.6 * cm, 2.6 * cm, 2.2 * cm, 1.8 * cm, 2.1 * cm, 1.8 * cm, 2.1 * cm], extra=extra))
    if ctx["violation_totals"]:
        story.append(P("Most common violations across the exam", H2))
        story.append(_bar_chart(ctx["violation_totals"]))
    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()
