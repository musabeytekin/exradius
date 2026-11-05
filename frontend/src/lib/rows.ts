export type Row = { id: number; key: string; value: string };

let nextId = 1;
export const newRow = (key = "", value = ""): Row => ({ id: nextId++, key, value });
export const rowsFrom = (m: Record<string, string>): Row[] => Object.entries(m).map(([k, v]) => newRow(k, v));
export const rowsTo = (rows: Row[]): Record<string, string> =>
  Object.fromEntries(rows.filter((r) => r.key.trim()).map((r) => [r.key.trim(), r.value]));
