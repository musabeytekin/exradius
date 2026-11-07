/** Matches the API's Slug type: ^[a-z0-9][a-z0-9._-]*$ */
export const SLUG_RE = /^[a-z0-9][a-z0-9._-]*$/;

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
