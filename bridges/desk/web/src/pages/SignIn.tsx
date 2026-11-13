import { Logo } from "@/components/Shell";
import { Problem } from "@/components/ui";
import { buttonClass } from "@/lib/styles";

export function SignIn({ error, next }: { error?: string; next?: string }) {
  const href = `/auth/login${next ? `?${new URLSearchParams({ next })}` : ""}`;
  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <div className="w-full max-w-sm space-y-5 rounded-xl border border-border bg-surface p-6 shadow-pop">
        <div className="flex items-center gap-2.5">
          <Logo />
          <h1 className="text-lg font-semibold tracking-[-0.01em]">Exradius Desk</h1>
        </div>
        {error && <Problem>{error}</Problem>}
        <a href={href} className={`${buttonClass("primary")} w-full`}>Sign in</a>
      </div>
    </div>
  );
}
