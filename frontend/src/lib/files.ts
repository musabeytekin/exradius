import type { AttachmentIn } from "./types";

/** A file picked in the browser, as an inline task or answer attachment. */
export function fileToAttachment(file: File): Promise<AttachmentIn> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error ?? new Error(`Could not read ${file.name}`));
    reader.onload = () => {
      const url = String(reader.result);
      resolve({
        name: file.name.replace(/[/\\]/g, "_"),
        content_type: file.type || "application/octet-stream",
        content_base64: url.slice(url.indexOf(",") + 1),
      });
    };
    reader.readAsDataURL(file);
  });
}

/** For previews of the published JSON: base64 bodies shortened. */
export function elideBase64<T>(value: T): T {
  return JSON.parse(
    JSON.stringify(value, (k, v) => (k === "content_base64" && typeof v === "string" && v.length > 48 ? `${v.slice(0, 32)}… (${v.length.toLocaleString()} chars)` : v)),
  ) as T;
}
