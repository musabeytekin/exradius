"""Writes docs/assets/flow-light.svg and flow-dark.svg, the README's animated diagram: python3 docs/assets/flow.py docs/assets"""
import sys
from pathlib import Path

THEMES = {
    "light": dict(card="#F6F8FA", border="#D0D7DE", box="#FFFFFF", text="#1F2328", muted="#59636E",
                  accent="#6D5EF6", soft="#EEEBFF", line="#8C959F", ok="#1A7F37"),
    "dark": dict(card="#161B22", border="#30363D", box="#0D1117", text="#E6EDF3", muted="#9198A1",
                 accent="#8B7FFF", soft="#221E3F", line="#6E7681", ok="#3FB950"),
}
SANS = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
DUR = "9s"


def window(s, e, edge=0.006):
    """Opacity values/keyTimes visible between s and e of the loop."""
    return "0;0;1;1;0;0", f"0;{s};{s + edge:.3f};{e};{e + edge:.3f};1"


def dot(path, s, e, c):
    vals, kt = window(s, e)
    return f"""
  <g class="anim" opacity="0">
    <circle r="11" fill="{c['accent']}" opacity="0.25"/>
    <circle r="6" fill="{c['accent']}"/>
    <animateMotion path="{path}" dur="{DUR}" repeatCount="indefinite" calcMode="linear"
                   keyPoints="0;0;1;1" keyTimes="0;{s};{e};1"/>
    <animate attributeName="opacity" values="{vals}" keyTimes="{kt}" dur="{DUR}" repeatCount="indefinite"/>
  </g>"""


def box(x, y, w, h, title, sub, c, *, mono_sub=False, title_size=15, fill=None, stroke=None):
    cx = x + w / 2
    ty = y + h / 2 - (2 if sub else -5)
    out = f"""
  <rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="{fill or c['box']}" stroke="{stroke or c['border']}" stroke-width="1.5"/>
  <text x="{cx}" y="{ty}" text-anchor="middle" font-size="{title_size}" font-weight="600" fill="{c['text']}">{title}</text>"""
    if sub:
        fam = MONO if mono_sub else SANS
        out += f"""
  <text x="{cx}" y="{ty + 19}" text-anchor="middle" font-size="12" font-family="{fam}" fill="{c['muted']}">{sub}</text>"""
    return out


def pill(x, y, w, label, c):
    return f"""
  <rect x="{x}" y="{y}" width="{w}" height="34" rx="17" fill="{c['soft']}" stroke="{c['accent']}" stroke-width="1.5"/>
  <text x="{x + w / 2}" y="{y + 22}" text-anchor="middle" font-size="12.5" font-family="{MONO}" fill="{c['text']}">{label}</text>"""


def chip(y, n, label, detail, c, s, e, *, agent=False):
    vals, kt = window(s, e)
    fill = c["soft"] if agent else c["box"]
    return f"""
  <rect x="690" y="{y}" width="220" height="36" rx="10" fill="{fill}" stroke="{c['border']}" stroke-width="1.2"/>
  <rect class="anim" x="690" y="{y}" width="220" height="36" rx="10" fill="{c['accent']}" fill-opacity="0.16" stroke="{c['accent']}" stroke-width="2" opacity="0">
    <animate attributeName="opacity" values="{vals}" keyTimes="{kt}" dur="{DUR}" repeatCount="indefinite"/>
  </rect>
  <text x="704" y="{y + 23}" font-size="12" font-weight="700" fill="{c['accent']}">{n}</text>
  <text x="720" y="{y + 23}" font-size="13.5" font-weight="600" fill="{c['text']}">{label}</text>
  <text x="898" y="{y + 23}" text-anchor="end" font-size="11.5" font-family="{MONO}" fill="{c['muted']}">{detail}</text>"""


def svg(theme):
    c = THEMES[theme]
    arrow = f'marker-end="url(#arrow-{theme})"'
    line = f'fill="none" stroke="{c["line"]}" stroke-width="1.8"'
    vals_ok, kt_ok = window(0.955, 0.994)
    parts = [f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 960 440" width="960" height="440" font-family="{SANS}" role="img" aria-labelledby="t d">
  <title id="t">How a task runs in Exradius</title>
  <desc id="d">A tracker publishes a task on exradius.tasks. The worker reads the project's exradius.yaml from its config repository and creates a Run; the operator turns it into one pod that clones the repositories, starts the services, runs setup and runs the Claude Code agent. Every request from the pod leaves through the egress proxy. Status, questions and the result go back to the tracker on exradius.events.</desc>
  <style>@media (prefers-reduced-motion: reduce) {{ .anim {{ display: none; }} }}</style>
  <defs>
    <marker id="arrow-{theme}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M0 0 L10 5 L0 10 z" fill="{c['line']}"/>
    </marker>
  </defs>
  <rect x="1" y="1" width="958" height="438" rx="16" fill="{c['card']}" stroke="{c['border']}"/>
"""]
    # Connections first, boxes on top.
    parts.append(f"""
  <path d="M180 140 H366" {line} {arrow}/>
  <path d="M435 70 V97" {line} stroke-dasharray="4 4" {arrow}/>
  <path d="M500 140 H527" {line} {arrow}/>
  <path d="M640 140 H667" {line} {arrow}/>
  <path d="M800 240 V297" {line} {arrow}/>
  <path d="M800 350 V369" {line} {arrow}/>
  <path d="M435 180 V350 H105 V189" {line} {arrow}/>""")
    # The tracker, with a green outline when the result arrives.
    parts.append(box(30, 95, 150, 90, "Your tracker", "Jira · GitHub · CLI", c))
    parts.append(f"""
  <rect class="anim" x="30" y="95" width="150" height="90" rx="12" fill="none" stroke="{c['ok']}" stroke-width="2.5" opacity="0">
    <animate attributeName="opacity" values="{vals_ok}" keyTimes="{kt_ok}" dur="{DUR}" repeatCount="indefinite"/>
  </rect>""")
    parts.append(pill(208, 123, 134, "exradius.tasks", c))
    parts.append(box(360, 20, 150, 50, "Config repository", "exradius.yaml", c, mono_sub=True, title_size=13.5))
    parts.append(box(370, 100, 130, 80, "Worker", "intake · state", c))
    parts.append(box(530, 110, 110, 60, "Operator", "Run → pod", c, title_size=14))
    # The run pod: a boundary with one way out, at the bottom, towards the egress proxy.
    parts.append(f"""
  <path d="M776 280 H688 Q670 280 670 262 V48 Q670 30 688 30 H912 Q930 30 930 48 V262 Q930 280 912 280 H824"
        fill="{c['accent']}" fill-opacity="0.05" stroke="{c['accent']}" stroke-width="2"/>
  <text x="690" y="56" font-size="14.5" font-weight="700" fill="{c['text']}">Run pod</text>
  <text x="910" y="56" text-anchor="end" font-size="12" fill="{c['muted']}">one per task, isolated</text>""")
    parts.append(chip(72, "1", "Clone repositories", "git", c, 0.24, 0.33))
    parts.append(chip(116, "2", "Start services", "sidecars", c, 0.33, 0.42))
    parts.append(chip(160, "3", "Run setup", "pnpm install", c, 0.42, 0.51))
    parts.append(chip(204, "4", "Claude Code agent", "/work", c, 0.51, 0.79, agent=True))
    parts.append(box(700, 300, 200, 50, "Egress proxy", "allowlist · adds credentials", c, title_size=13.5,
                     stroke=c["accent"]))
    parts.append(box(700, 372, 200, 44, "Git · model API · packages", None, c, title_size=12.5))
    parts.append(pill(208, 333, 134, "exradius.events", c))
    parts.append(f"""
  <text x="275" y="388" text-anchor="middle" font-size="12" fill="{c['muted']}">status · questions · result</text>
  <text x="275" y="116" text-anchor="middle" font-size="12" fill="{c['muted']}">task</text>""")
    # The moving parts.
    parts.append(dot("M180 140 H366", 0.02, 0.12, c))
    parts.append(dot("M500 140 H667", 0.13, 0.23, c))
    parts.append(dot("M800 240 V370", 0.58, 0.70, c))
    parts.append(dot("M435 180 V350 H105 V189", 0.80, 0.955, c))
    parts.append("\n</svg>\n")
    return "".join(parts)


out = Path(sys.argv[1])
for theme in THEMES:
    (out / f"flow-{theme}.svg").write_text(svg(theme))
