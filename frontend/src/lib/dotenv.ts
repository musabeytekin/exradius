import { SECRET_KEY_RE } from "./projectConfig";

export type DotenvResult = { values: Record<string, string>; error?: undefined } | { values?: undefined; error: string };

/** Parse `KEY=value` lines. Blank lines and `#` comments are skipped; a later line wins over an earlier one. */
export function parseDotenv(text: string): DotenvResult {
  const values: Record<string, string> = {};
  const lines = text.split(/\r?\n/);
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    if (!line || line.startsWith("#")) continue;
    const n = i + 1;
    const eq = line.indexOf("=");
    if (eq < 0) return { error: `Line ${n} has no "=".` };
    const key = line.slice(0, eq).trim();
    let value = line.slice(eq + 1).trim();
    if (!key) return { error: `Line ${n} has no key name.` };
    if (!SECRET_KEY_RE.test(key)) return { error: `Line ${n}: ${key} is not a valid key name; use letters, digits, dot, dash and underscore.` };
    if (value.length >= 2 && (value[0] === '"' || value[0] === "'") && value.at(-1) === value[0]) value = value.slice(1, -1);
    if (!value) return { error: `Line ${n}: ${key} has no value.` };
    values[key] = value;
  }
  return { values };
}
