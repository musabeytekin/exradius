import { useNavigate } from "@tanstack/react-router";
import type { AnchorHTMLAttributes } from "react";

/** A link to a path with a query string, routed client-side. */
export function HrefLink({ href, onClick, ...rest }: AnchorHTMLAttributes<HTMLAnchorElement> & { href: string }) {
  const navigate = useNavigate();
  return (
    <a
      href={href}
      onClick={(e) => {
        onClick?.(e);
        if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
        e.preventDefault();
        navigate({ href });
      }}
      {...rest}
    />
  );
}
