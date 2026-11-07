import { useQueries } from "@tanstack/react-query";
import { api } from "./api";
import { keyName } from "./projectConfig";
import { qk, useStores } from "./queries";

/**
 * Key names per store, for computing project health against the live stores:
 * `string[]` when known, `null` when no store has that name, `undefined` while unknown
 * (stores not loaded, or not available on this API).
 */
export function useStoreKeyIndex(): (name: string | null) => string[] | null | undefined {
  const stores = useStores();
  const list = stores.data ?? [];
  const keys = useQueries({
    queries: list.map((s) => ({ queryKey: qk.storeKeys(s.id), queryFn: () => api.stores.keys(s.id) })),
  });
  return (name) => {
    if (!name || !stores.isSuccess) return undefined;
    const i = list.findIndex((s) => s.name === name);
    if (i < 0) return null;
    const data = keys[i]?.data;
    return data ? data.map(keyName) : undefined;
  };
}
