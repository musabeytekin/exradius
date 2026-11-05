import { useQueryClient } from "@tanstack/react-query";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { useState } from "react";
import { Logo } from "@/components/AppShell";
import { Button, Field, Input } from "@/components/ui/primitives";
import { api, ApiError } from "@/lib/api";
import { qk } from "@/lib/queries";

export function LoginPage() {
  const { next } = useSearch({ from: "/login" });
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const me = await api.auth.login(username, password);
      qc.setQueryData(qk.me, me);
      navigate({ to: next && next.startsWith("/") && !next.startsWith("//") ? next : "/" });
    } catch (err) {
      setError(err instanceof ApiError && err.status === 401 ? "That username and password do not match." : (err as Error).message);
      setPassword("");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="relative flex min-h-screen items-center justify-center overflow-hidden bg-bg px-4">
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(60%_50%_at_50%_0%,var(--accent-soft),transparent)]" />
      <div className="pointer-events-none absolute inset-0 opacity-[0.35] [background-image:linear-gradient(var(--border)_1px,transparent_1px),linear-gradient(90deg,var(--border)_1px,transparent_1px)] [background-size:32px_32px] [mask-image:radial-gradient(50%_45%_at_50%_30%,black,transparent)]" />
      <form onSubmit={submit} className="relative w-[360px] animate-pop-in">
        <div className="mb-8 flex flex-col items-center gap-3 text-center">
          <Logo size={36} />
          <div>
            <h1 className="text-lg font-semibold tracking-[-0.01em]">Exradius</h1>
            <p className="text-sm text-muted">Sign in to the admin panel</p>
          </div>
        </div>
        <div className="flex flex-col gap-4 rounded-xl border border-border bg-surface p-5 shadow-pop">
          <Field label="Username" htmlFor="username">
            <Input id="username" value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" required />
          </Field>
          <Field label="Password" htmlFor="password" error={error}>
            <Input
              id="password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              autoFocus
              required
              invalid={!!error}
            />
          </Field>
          <Button type="submit" variant="primary" loading={busy} className="mt-1 w-full">Sign in</Button>
        </div>
        <p className="mt-5 text-center text-xs text-subtle">
          Sign in with the admin account created at installation. Change its password under Settings.
        </p>
      </form>
    </div>
  );
}
