"""Writes docs/assets/flow-light.svg and flow-dark.svg, the README's animated diagram of what
happens to a ticket once it is assigned to the agent, and where Exradius decides.

    python3 docs/assets/flow.py docs/assets
"""
import sys
from pathlib import Path

THEMES = {
    "light": dict(card="#F6F8FA", border="#D0D7DE", box="#FFFFFF", text="#1F2328", muted="#59636E",
                  accent="#6D5EF6", soft="#EEEBFF", line="#8C959F", ok="#1A7F37", ok_soft="#DAFBE1",
                  no="#CF222E", no_soft="#FFEBE9"),
    "dark": dict(card="#161B22", border="#30363D", box="#0D1117", text="#E6EDF3", muted="#9198A1",
                 accent="#8B7FFF", soft="#221E3F", line="#6E7681", ok="#3FB950", ok_soft="#12261E",
                 no="#F85149", no_soft="#2D1517"),
}
SANS = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
DUR = "14s"
EDGE = 0.006


def window(s, e):
    """Opacity values and keyTimes for something shown between s and e of the loop."""
    return "0;0;1;1;0;0", f"0;{s};{s + EDGE:.3f};{e};{e + EDGE:.3f};1"


def blink(s, e):
    vals, kt = window(s, e)
    return f'<animate attributeName="opacity" values="{vals}" keyTimes="{kt}" dur="{DUR}" repeatCount="indefinite"/>'


def lines(cx, y, texts, c, *, size=13, weight=400, color=None, anchor="middle", gap=17):
    out = []
    for i, t in enumerate(texts):
        out.append(f'<text x="{cx}" y="{y + i * gap}" text-anchor="{anchor}" font-size="{size}" '
                   f'font-weight="{weight}" fill="{color or c["text"]}">{t}</text>')
    return "\n  ".join(out)


def rect(x, y, w, h, c, *, fill=None, stroke=None, rx=12, width=1.5, extra=""):
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill or c["box"]}" '
            f'stroke="{stroke or c["border"]}" stroke-width="{width}"{extra}/>')


def glow_rect(x, y, w, h, c, s, e, *, color=None, rx=12):
    col = color or c["accent"]
    return (f'<rect class="anim" x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{col}" fill-opacity="0.14" '
            f'stroke="{col}" stroke-width="2.5" opacity="0">{blink(s, e)}</rect>')


def diamond(cx, cy, label, c, s, e, hw=72, hh=50):
    pts = f"{cx},{cy - hh} {cx + hw},{cy} {cx},{cy + hh} {cx - hw},{cy}"
    return f"""
  <polygon points="{pts}" fill="{c['box']}" stroke="{c['accent']}" stroke-width="1.8" stroke-linejoin="round"/>
  <polygon class="anim" points="{pts}" fill="{c['accent']}" fill-opacity="0.16" stroke="{c['accent']}" stroke-width="3" stroke-linejoin="round" opacity="0">{blink(s, e)}</polygon>
  {lines(cx, cy - 3, label, c, size=13.5, weight=600)}"""


def node(x, y, w, h, title, sub, c, s, e, *, fill=None, stroke=None, glow=None):
    cx = x + w / 2
    n = len(title) + len(sub)
    top = y + h / 2 - (n - 1) * 8.5 + 4
    return f"""
  {rect(x, y, w, h, c, fill=fill, stroke=stroke)}
  {glow_rect(x, y, w, h, c, s, e, color=glow)}
  {lines(cx, top, title, c, size=14, weight=600)}
  {lines(cx, top + len(title) * 17, sub, c, size=12, color=c['muted'])}"""


def outcome(x, y, w, h, title, sub, c, *, good=False):
    """A side exit: where a ticket leaves the main path, and what the person sees."""
    col, soft = (c["ok"], c["ok_soft"]) if good else (c["no"], c["no_soft"])
    cx = x + w / 2
    return f"""
  {rect(x, y, w, h, c, fill=soft, stroke=col, rx=10, width=1.2)}
  {lines(cx, y + 21, [title], c, size=12.5, weight=600)}
  {lines(cx, y + 38, [sub], c, size=11.5, color=c['muted'])}"""


def label(x, y, text, c, anchor="middle"):
    return f'<text x="{x}" y="{y}" text-anchor="{anchor}" font-size="11.5" font-weight="600" fill="{c["muted"]}">{text}</text>'


def svg(theme):
    c = THEMES[theme]
    arrow = f'marker-end="url(#arrow-{theme})"'
    line = f'fill="none" stroke="{c["line"]}" stroke-width="1.8"'
    no_line = f'fill="none" stroke="{c["no"]}" stroke-width="1.5" stroke-dasharray="4 3"'
    p = []
    p.append(f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 960 580" width="960" height="580" font-family="{SANS}" role="img" aria-labelledby="t d">
  <title id="t">What happens to a ticket assigned to Exradius</title>
  <desc id="d">You assign a ticket to the agent. Exradius checks which project it belongs to, whether that project is ready, and which agent should take it; at each step a ticket that cannot go on is answered with the reason. The chosen agent gets a fresh workstation with the code, services and setup, works on the ticket, asks on the ticket when information is missing, and delivers a branch and pull request, or a patch when it may not push. The result is posted on the ticket and the ticket comes back to you.</desc>
  <style>@media (prefers-reduced-motion: reduce) {{ .anim {{ display: none; }} }}</style>
  <defs>
    <marker id="arrow-{theme}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M0 0 L10 5 L0 10 z" fill="{c['line']}"/>
    </marker>
    <marker id="no-{theme}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
      <path d="M0 0 L10 5 L0 10 z" fill="{c['no']}"/>
    </marker>
  </defs>
  <rect x="1" y="1" width="958" height="578" rx="16" fill="{c['card']}" stroke="{c['border']}"/>
""")
    # The main path, and the side exits.
    p.append(f"""
  <path d="M170 170 H186" {line} {arrow}/>
  <path d="M332 170 H376" {line} {arrow}/>
  <path d="M522 170 H566" {line} {arrow}/>
  <path d="M640 220 V358" {line} {arrow}/>
  <path d="M520 400 H492" {line} {arrow}/>
  <path d="M320 400 H302" {line} {arrow}/>
  <path d="M156 400 H142" {line} {arrow}/>
  <path d="M260 120 V90" {no_line} marker-end="url(#no-{theme})"/>
  <path d="M450 120 V90" {no_line} marker-end="url(#no-{theme})"/>
  <path d="M640 120 V90" {no_line} marker-end="url(#no-{theme})"/>
  <path d="M640 440 V478" {no_line} marker-end="url(#no-{theme})"/>
  <path d="M712 170 H758" fill="none" stroke="{c['accent']}" stroke-width="1.5" stroke-dasharray="2 4"/>
  <path d="M395 444 V476" {line} {arrow}/>
  <path d="M415 478 V446" {line} {arrow}/>
  <path d="M228 450 V478" {line} {arrow}/>
  {label(354, 162, "found", c)}
  {label(544, 162, "yes", c)}
  {label(652, 300, "chosen", c, anchor="start")}
  {label(272, 106, "none", c, anchor="start")}
  {label(462, 106, "no", c, anchor="start")}
  {label(652, 106, "none", c, anchor="start")}""")
    # Row 1: the ticket arrives and Exradius decides whether and by whom it is worked on.
    p.append(node(20, 132, 150, 76, ["You assign a ticket"], ["to the agent"], c, 0.0, 0.05,
                  fill=c["soft"], stroke=c["accent"]))
    p.append(outcome(180, 34, 160, 54, "Not an Exradius project", "the ticket says so", c))
    p.append(diamond(260, 170, ["Which", "project?"], c, 0.085, 0.14))
    p.append(outcome(370, 34, 160, 54, "Config or secret missing", "refused, with the reason", c))
    p.append(diamond(450, 170, ["Project", "ready?"], c, 0.175, 0.23))
    p.append(outcome(560, 34, 160, 54, "No agent can be chosen", "refused, with the reason", c))
    p.append(diamond(640, 170, ["Which", "agent?"], c, 0.265, 0.34))
    # How the agent is picked.
    p.append(f"""
  {rect(760, 104, 180, 136, c, rx=10)}
  {glow_rect(760, 104, 180, 136, c, 0.265, 0.34, rx=10)}
  {lines(774, 126, ["How it picks"], c, size=12.5, weight=700, color=c['accent'], anchor="start")}
  {lines(774, 146, ["Named on the ticket: that one.", "Otherwise it compares the", "ticket with each agent's role", "and tools, and takes the best", "match. Unsure? It still runs,", "and says that it guessed."], c, size=11.5, color=c['muted'], anchor="start", gap=15.5)}""")
    # Row 2: the work, right to left.
    p.append(node(520, 360, 240, 80, ["A workstation of its own"], ["code cloned, services started,", "setup run"], c,
                  0.38, 0.46))
    p.append(outcome(545, 480, 215, 54, "Setup fails", "the task stops, naming the cause", c))
    p.append(node(320, 356, 170, 88, ["The agent works"], ["on the ticket, reaching", "only allowed sites"], c, 0.50, 0.70))
    p.append(f"""
  {rect(290, 478, 230, 70, c, rx=10)}
  {glow_rect(290, 478, 230, 70, c, 0.555, 0.645, rx=10)}
  {lines(405, 499, ["Missing information?"], c, size=12.5, weight=600)}
  {lines(405, 517, ["It asks on the ticket and waits;", "your reply picks up where it stopped"], c, size=11.5, color=c['muted'], gap=15)}""")
    p.append(diamond(228, 400, ["May it", "push?"], c, 0.735, 0.79))
    p.append(f"""
  {rect(130, 478, 150, 70, c, rx=10)}
  {glow_rect(130, 478, 150, 70, c, 0.735, 0.79, rx=10)}
  {lines(144, 499, ["Yes: a branch and", "a pull request", "No: a patch instead"], c, size=11.5, color=c['muted'], anchor="start", gap=16)}""")
    p.append(node(20, 352, 122, 96, ["Result on", "the ticket"], ["back to you", "to review"], c, 0.83, 0.97,
                  fill=c["ok_soft"], stroke=c["ok"], glow=c["ok"]))
    # The ticket's journey: a dot that stops at each decision.
    main = "M170 170 H640 V400 H142"
    kt = "0;0.05;0.085;0.14;0.175;0.23;0.265;0.34;0.38;0.46;0.50;0.70;0.735;0.79;0.83;1"
    total = 470 + 230 + 498
    def at(d):
        return f"{d / total:.4f}"
    kp = ";".join([at(0), at(0), at(90), at(90), at(280), at(280), at(470), at(470), at(700), at(700),
                   at(700 + 235), at(700 + 235), at(700 + 412), at(700 + 412), at(total), at(total)])
    vals, kto = window(0.0, 0.835)
    p.append(f"""
  <g class="anim" opacity="0">
    <circle r="12" fill="{c['accent']}" opacity="0.25"/>
    <circle r="6.5" fill="{c['accent']}"/>
    <animateMotion path="{main}" dur="{DUR}" repeatCount="indefinite" calcMode="linear" keyPoints="{kp}" keyTimes="{kt}"/>
    <animate attributeName="opacity" values="{vals}" keyTimes="{kto}" dur="{DUR}" repeatCount="indefinite"/>
  </g>""")
    # The question going out to the ticket and the answer coming back.
    vals, kto = window(0.555, 0.645)
    p.append(f"""
  <g class="anim" opacity="0">
    <circle r="5" fill="{c['accent']}"/>
    <animateMotion path="M395 444 V478 H415 V444" dur="{DUR}" repeatCount="indefinite" calcMode="linear"
                   keyPoints="0;0;0.5;0.5;1;1" keyTimes="0;0.555;0.58;0.62;0.645;1"/>
    <animate attributeName="opacity" values="{vals}" keyTimes="{kto}" dur="{DUR}" repeatCount="indefinite"/>
  </g>""")
    p.append("\n</svg>\n")
    return "".join(p)


out = Path(sys.argv[1])
for theme in THEMES:
    (out / f"flow-{theme}.svg").write_text(svg(theme))
