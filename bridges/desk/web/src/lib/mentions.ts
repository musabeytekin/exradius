// Mentions are stored as references (`@[person:7]`, `@[exradius]`) and rendered with
// today's names (design §8). In the comment box they are typed as `@Name`.

export const TOKEN = /@\[(?:person:(\d+)|(exradius))\]/g;

export interface Candidate {
  token: string;
  name: string;
  exradius?: boolean;
}

export type Piece = { text: string } | { mention: string; exradius: boolean };

/** A body split into text and mentions, with each mention's current name. */
export function pieces(body: string, names: Record<string, string>, exradiusName = "Exradius"): Piece[] {
  const out: Piece[] = [];
  let last = 0;
  for (const m of body.matchAll(TOKEN)) {
    if (m.index > last) out.push({ text: body.slice(last, m.index) });
    out.push(m[2] ? { mention: exradiusName, exradius: true } : { mention: names[m[1]] ?? "someone", exradius: false });
    last = m.index + m[0].length;
  }
  if (last < body.length) out.push({ text: body.slice(last) });
  return out;
}

/** The `@word` being typed at the caret, if any: where it starts and what is typed so far. */
export function typing(text: string, caret: number): { start: number; query: string } | null {
  const before = text.slice(0, caret);
  const m = /(^|\s)@([^\s@]{0,30})$/.exec(before);
  if (!m) return null;
  return { start: caret - m[2].length - 1, query: m[2] };
}

export function matching(candidates: Candidate[], query: string): Candidate[] {
  const q = query.toLowerCase();
  return candidates.filter((c) => c.name.toLowerCase().split(/\s+/).some((w) => w.startsWith(q))
    || c.name.toLowerCase().startsWith(q)).slice(0, 8);
}

/** The typed text with each chosen `@Name` turned into its reference, longest names first. */
export function encode(text: string, chosen: Candidate[]): string {
  const unique = [...new Map(chosen.map((c) => [c.name, c])).values()].sort((a, b) => b.name.length - a.name.length);
  let out = text;
  for (const c of unique) {
    const escaped = c.name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    out = out.replace(new RegExp(`@${escaped}(?![\\w])`, "g"), c.token);
  }
  return out;
}
