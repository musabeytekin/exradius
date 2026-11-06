// Agent identity (docs/agent-identity.md): who the agent is when it commits.

/** The migration backfills existing agents with `<slug>@exradius.invalid`; those need a real email. */
export const PLACEHOLDER_DOMAIN = "@exradius.invalid";

export function needsEmail(agent: { identity_email?: string | null }): boolean {
  return typeof agent.identity_email === "string" && (!agent.identity_email.trim() || agent.identity_email.trim().toLowerCase().endsWith(PLACEHOLDER_DOMAIN));
}

export function emailError(email: string): string | null {
  const e = email.trim();
  if (!e) return null;
  if (/[<>]/.test(e)) return "Just the address, without angle brackets.";
  if (!/^[^\s@]+@[^\s@]+$/.test(e)) return "Not an email address.";
  if (e.toLowerCase().endsWith(PLACEHOLDER_DOMAIN)) return "A placeholder from the migration. Set the address commits should carry.";
  return null;
}
