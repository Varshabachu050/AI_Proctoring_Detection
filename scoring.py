"""
Violation catalogue, cheating/misuse percentage, and the written analysis.

Misuse model (transparent on purpose, so examiners can explain it):

  points = sum over violation types of  weight * min(count, cap)
  misuse% = 100 * (1 - exp(-points / 60))

* `weight` reflects how serious one incident is (a phone matters more than a
  brief glance away).
* `cap` stops one noisy detector from drowning everything else.
* The exponential curve saturates smoothly towards 100% and never exceeds it.

  points  15 -> 22%     points  30 -> 39%     points  60 -> 63%     points 120 -> 86%

Risk level: Low < 25%, Medium 25-50%, High >= 50%.
AI flags are indicators for human review, not proof of cheating.
"""
import math

# key: (label, weight per incident, cap on counted incidents, hex colour)
VIOLATION_TYPES = {
    "no_face":         ("Face not visible",               6, 8,  "#f59e0b"),
    "multiple_faces":  ("Multiple faces / people",       10, 6,  "#ef4444"),
    "looking_away":    ("Looking away from screen",       4, 10, "#f97316"),
    "head_movement":   ("Suspicious head movement",       4, 6,  "#eab308"),
    "talking":         ("Talking / voice detected",       5, 8,  "#8b5cf6"),
    "phone":           ("Mobile phone detected",         15, 4,  "#dc2626"),
    "object":          ("Unauthorized object detected",   8, 5,  "#db2777"),
    "tab_switch":      ("Tab / window switch",            5, 8,  "#0ea5e9"),
    "fullscreen_exit": ("Fullscreen exited",              4, 5,  "#6366f1"),
    "copy_paste":      ("Copy / paste / shortcut attempt", 3, 5, "#14b8a6"),
    "camera_off":      ("Camera or microphone lost",     15, 3,  "#991b1b"),
}

SCALE = 60.0
LOW_MAX = 25.0
MEDIUM_MAX = 50.0


def label(vtype):
    return VIOLATION_TYPES.get(vtype, (vtype.replace("_", " ").title(),))[0]


def color(vtype):
    return VIOLATION_TYPES.get(vtype, (None, 0, 0, "#64748b"))[3]


def counts_from(violations):
    counts = {}
    for v in violations:
        counts[v["vtype"]] = counts.get(v["vtype"], 0) + 1
    return counts


def risk_level(pct):
    if pct < LOW_MAX:
        return "Low"
    if pct < MEDIUM_MAX:
        return "Medium"
    return "High"


def compute_misuse(counts):
    """counts: {vtype: n}  ->  (points, misuse_pct, risk_level)"""
    points = 0.0
    for vtype, n in counts.items():
        meta = VIOLATION_TYPES.get(vtype)
        if not meta:
            continue
        _, weight, cap, _ = meta
        points += weight * min(n, cap)
    pct = round(100.0 * (1.0 - math.exp(-points / SCALE)), 1)
    return points, pct, risk_level(pct)


def breakdown(counts):
    """Rows for tables/charts, sorted by points contributed (desc)."""
    rows = []
    for vtype, n in counts.items():
        meta = VIOLATION_TYPES.get(vtype)
        if not meta:
            continue
        lbl, weight, cap, col = meta
        rows.append({
            "vtype": vtype, "label": lbl, "count": n, "weight": weight,
            "cap": cap, "points": weight * min(n, cap), "color": col,
        })
    rows.sort(key=lambda r: (-r["points"], -r["count"]))
    return rows


def timeline_buckets(violations, duration_sec, buckets=10):
    """Violation counts spread across the attempt, for the activity chart."""
    duration_sec = max(int(duration_sec), 1)
    out = [0] * buckets
    for v in violations:
        idx = min(buckets - 1, int(v["offset_sec"] * buckets / duration_sec))
        out[max(idx, 0)] += 1
    return out


def analyze(violations, duration_sec, pct, level, pending_text=0):
    """Plain-English findings used on the dashboard and in the PDF."""
    notes = []
    total = len(violations)
    counts = counts_from(violations)

    if total == 0:
        notes.append("No suspicious activity was detected during this attempt.")
    else:
        rows = breakdown(counts)
        top = rows[0]
        notes.append(
            f"{total} incident(s) were recorded across {len(rows)} category(ies). "
            f"The largest contributor was '{top['label']}' "
            f"({top['count']} incident(s), {top['points']:g} risk points)."
        )
        if counts.get("phone"):
            notes.append(f"A mobile phone was detected {counts['phone']} time(s) - review the screenshots.")
        if counts.get("multiple_faces"):
            notes.append(
                f"More than one person appeared in frame {counts['multiple_faces']} time(s), "
                "which may indicate outside assistance."
            )
        if counts.get("object"):
            notes.append(f"Unauthorized objects (e.g. books, extra devices) were flagged {counts['object']} time(s).")
        browser = sum(counts.get(k, 0) for k in ("tab_switch", "fullscreen_exit", "copy_paste"))
        if browser:
            notes.append(f"{browser} browser-integrity event(s) occurred (tab switches, fullscreen exits, copy/paste attempts).")
        if counts.get("camera_off"):
            notes.append("The camera or microphone was interrupted - the student may have tried to disable monitoring.")
        behaviour = sum(counts.get(k, 0) for k in ("looking_away", "head_movement", "talking", "no_face"))
        if behaviour >= 6:
            notes.append("Frequent behavioural flags (looking away, talking, leaving the frame) suggest sustained distraction or external reference.")

        if duration_sec > 0 and total >= 3:
            thirds = [0, 0, 0]
            for v in violations:
                thirds[min(2, int(v["offset_sec"] * 3 / max(duration_sec, 1)))] += 1
            peak = max(range(3), key=lambda i: thirds[i])
            if thirds[peak] >= max(3, total * 0.5):
                name = ["first", "middle", "final"][peak]
                notes.append(f"Most incidents ({thirds[peak]} of {total}) clustered in the {name} third of the exam.")

    verdicts = {
        "Low": "Overall risk is LOW; no further action is normally needed.",
        "Medium": "Overall risk is MEDIUM; a manual review of the evidence screenshots is recommended.",
        "High": "Overall risk is HIGH; reviewing the evidence and following up with the student is strongly recommended.",
    }
    notes.append(f"Cheating / misuse likelihood: {pct:g}%. {verdicts[level]}")
    if pending_text:
        notes.append(f"{pending_text} descriptive answer(s) still await manual grading.")
    notes.append("Note: AI detections are indicators for human review, not proof of misconduct.")
    return notes
