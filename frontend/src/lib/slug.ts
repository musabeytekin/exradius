/** Matches the API's Slug type: ^[a-z0-9][a-z0-9._-]*$ */
export const SLUG_RE = /^[a-z0-9][a-z0-9._-]*$/;
/** Matches the API's EnvKey type. */
export const ENV_KEY_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;
/** Repository folder names. */
export const FOLDER_RE = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;

export function slugify(name: string): string {
  return name
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, "-")
    .replace(/-{2,}/g, "-")
    .replace(/^[-._]+|[-._]+$/g, "")
    .slice(0, 100);
}

/** Secret keys are uppercase: letters, digits and underscores. */
export function secretKey(input: string): string {
  return input.toUpperCase().replace(/[^A-Z0-9_]/g, "_");
}

/** Repository folder name from its URL's last path segment, without `.git`. */
export function repoNameFromUrl(url: string): string {
  const cleaned = url.trim().replace(/[?#].*$/, "").replace(/\/+$/, "");
  const last = cleaned.split(/[/:]/).pop() ?? "";
  return last.replace(/\.git$/i, "").replace(/[^A-Za-z0-9._-]/g, "-").replace(/^[^A-Za-z0-9]+/, "");
}

export function shortRandom(n = 6): string {
  const alphabet = "abcdefghijkmnpqrstuvwxyz23456789";
  const values = crypto.getRandomValues(new Uint8Array(n));
  return Array.from(values, (b) => alphabet[b % alphabet.length]).join("");
}
