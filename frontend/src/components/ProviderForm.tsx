import { useState } from "react";
import { PickList } from "@/components/ui/controls";
import { Dialog } from "@/components/ui/overlays";
import { Banner, Button, Field, Input } from "@/components/ui/primitives";
import { missingSettings, providerFormModel, settingLabel, settingsFor } from "@/lib/providers";
import { SLUG_RE, slugify } from "@/lib/slug";
import type { Provider, ProviderIn, ProviderKindSpec } from "@/lib/types";

/** A slug derived from the name that no other provider uses yet. */
function uniqueSlug(base: string, taken: string[]): string {
  const root = slugify(base) || "provider";
  if (!taken.includes(root)) return root;
  for (let i = 2; ; i++) if (!taken.includes(`${root}-${i}`)) return `${root}-${i}`;
}

/**
 * Create or edit a provider. Every field after the name comes from the capability block the
 * runtimes declare: kind, then auth method, then the credential and the method's settings.
 * The credential is write-only: it lives in this component's state until saved and is never
 * prefilled.
 */
export function ProviderDialog({
  open, onOpenChange, kinds, provider, takenSlugs, onSave,
}: {
  open: boolean; onOpenChange: (o: boolean) => void; kinds: ProviderKindSpec[];
  provider: Provider | null; takenSlugs: string[]; onSave: (body: ProviderIn) => Promise<void>;
}) {
  const [kindName, setKindName] = useState<string | null>(provider?.kind ?? null);
  const [methodName, setMethodName] = useState<string | null>(provider?.auth_method ?? null);
  const model = providerFormModel(kinds, kindName, methodName);
  const [credential, setCredential] = useState("");
  const [settings, setSettings] = useState<Record<string, string>>(() => settingsFor(model.method, provider?.settings ?? {}));
  const [name, setName] = useState(provider?.name ?? "");
  const [nameTouched, setNameTouched] = useState(!!provider);
  const [slug, setSlug] = useState(provider?.slug ?? "");
  const [slugTouched, setSlugTouched] = useState(!!provider);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const kind = model.kind;
  const method = model.method;
  const others = takenSlugs.filter((s) => s !== provider?.slug);
  const effectiveName = nameTouched ? name : kind?.label ?? "";
  const effectiveSlug = slugTouched ? slug : uniqueSlug(effectiveName, others);
  const slugError = !effectiveSlug ? null : !SLUG_RE.test(effectiveSlug) ? "Lowercase letters, digits, dot, dash and underscore." : others.includes(effectiveSlug) ? "Another provider uses this slug." : null;
  // A stored credential belongs to its auth method; switching method needs a new one.
  const methodChanged = !!provider && (kind?.kind !== provider.kind || method?.name !== provider.auth_method);
  const credentialRequired = !provider || methodChanged;
  const missing = missingSettings(method, settings);
  const ready = !!kind && !!method && !!effectiveName.trim() && !!effectiveSlug && !slugError && (!credentialRequired || !!credential.trim()) && missing.length === 0;

  const pickMethod = (kindValue: string | null, methodValue: string | null) => {
    const next = providerFormModel(kinds, kindValue, methodValue);
    setKindName(next.kind?.kind ?? null);
    setMethodName(next.method?.name ?? null);
    setSettings((cur) => settingsFor(next.method, cur));
  };

  const submit = async () => {
    if (!ready || !kind || !method || saving) return;
    setSaving(true);
    setError(null);
    const body: ProviderIn = {
      name: effectiveName.trim(), slug: effectiveSlug, kind: kind.kind, auth_method: method.name,
      settings: Object.fromEntries(Object.entries(settings).filter(([, v]) => v.trim() !== "")),
    };
    if (credential.trim()) body.credential = credential.trim();
    try {
      await onSave(body);
      setCredential("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      width={560}
      title={provider ? `Edit ${provider.name}` : "New provider"}
      description="The credential a runtime injects into each run. It is tested when you save."
      onSubmit={submit}
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button type="submit" variant="primary" icon="play" loading={saving} disabled={!ready}>
            {provider ? "Save and test" : "Create and test"}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        {kinds.length === 0 ? (
          <Banner tone="warn" title="No provider kinds offered">No runtime on this installation declares a provider it accepts.</Banner>
        ) : (
          <>
            <div className="grid grid-cols-2 gap-3">
              <Field label="Kind">
                <PickList aria-label="Kind" value={kind?.kind ?? null} onChange={(v) => pickMethod(v, null)} options={model.kinds} />
              </Field>
              <Field label="Authentication">
                <PickList aria-label="Authentication" value={method?.name ?? null} onChange={(v) => pickMethod(kind?.kind ?? null, v)} options={model.authMethods} />
              </Field>
            </div>
            {model.notice && <Banner tone="warn" icon="info">{model.notice}</Banner>}
            <Field
              label={model.credentialLabel}
              htmlFor="provider-credential"
              hint={provider && !methodChanged ? "Stored and never shown. Leave empty to keep it." : "Write-only: it is encrypted at rest and never shown again."}
            >
              <Input
                id="provider-credential"
                type="password"
                mono
                autoComplete="new-password"
                spellCheck={false}
                autoFocus={!provider}
                value={credential}
                onChange={(e) => setCredential(e.target.value)}
                placeholder={provider && !methodChanged ? "••••••••  stored" : undefined}
              />
            </Field>
            {model.settings.map((f) => (
              <Field key={f.name} label={settingLabel(f)} optional={!f.required} hint={f.hint ?? undefined} htmlFor={`provider-setting-${f.name}`}>
                {f.options?.length ? (
                  <PickList
                    id={`provider-setting-${f.name}`}
                    aria-label={settingLabel(f)}
                    value={settings[f.name] || null}
                    onChange={(v) => setSettings((s) => ({ ...s, [f.name]: v ?? "" }))}
                    options={f.options.map((o) => ({ value: o, label: o }))}
                    noneLabel={f.required ? undefined : "Not set"}
                  />
                ) : (
                  <Input
                    id={`provider-setting-${f.name}`}
                    mono
                    value={settings[f.name] ?? ""}
                    placeholder={f.placeholder ?? undefined}
                    onChange={(e) => setSettings((s) => ({ ...s, [f.name]: e.target.value }))}
                  />
                )}
              </Field>
            ))}
            <div className="grid grid-cols-2 gap-3 border-t border-border pt-4">
              <Field label="Name" htmlFor="provider-name">
                <Input id="provider-name" value={effectiveName} onChange={(e) => { setNameTouched(true); setName(e.target.value); }} />
              </Field>
              <Field label="Slug" error={slugError} htmlFor="provider-slug">
                <Input id="provider-slug" mono value={effectiveSlug} invalid={!!slugError} onChange={(e) => { setSlugTouched(true); setSlug(e.target.value.toLowerCase()); }} />
              </Field>
            </div>
          </>
        )}
        {error && <Banner tone="err" title="Not saved">{error}</Banner>}
      </div>
    </Dialog>
  );
}
