// Persisted UI preferences: localStorage-backed `{read, write}` helpers.
// Every list toggle/sort/pick that survives a reload is a localStorage string
// wrapped in a try/catch (private mode throws, and the pref must then just stay
// in memory for the session). That read-with-catch / write-with-catch pair used
// to be hand-rolled six times across network-devices / network-wifi /
// network-survey. These own the storage concern only — value
// semantics (what an absent or unrecognised string means) stay at the call site.

/** `{read, write}` over one localStorage key. `read()` yields the raw stored
 *  string, or `fallback` when absent or unreadable; `write(value)` removes the
 *  key for a null/empty value and stores `String(value)` otherwise. */
export function persistedPref(key, fallback) {
  const missing = fallback === undefined ? null : fallback;
  return {
    read() {
      try {
        const raw = localStorage.getItem(key);
        return raw === null ? missing : raw;
      } catch (_e) {
        return missing;  // private mode — in-memory only
      }
    },
    write(value) {
      try {
        if (value == null || value === '') localStorage.removeItem(key);
        else localStorage.setItem(key, String(value));
      } catch (_e) { /* private mode — in-memory only */ }
    },
  };
}

/** `persistedPref` for an on/off flag stored as `'1'`/`'0'`. `read()` is a
 *  boolean (absent or unreadable → `fallback`, default `false`).
 *
 *  `read()` also accepts the legacy `'true'`/`'false'` spelling:
 *  the circuits / plugs / presence / security "show hidden" flags hand-rolled
 *  their own `localStorage` try/catch pair writing `String(bool)` before they
 *  moved onto this helper, and a browser that already has one of those values
 *  stored must keep its setting rather than silently reverting to the default.
 *  Writes are always the canonical `'1'`/`'0'`, so a key self-migrates the
 *  first time the user flips it. */
export function persistedFlag(key, fallback) {
  const pref = persistedPref(key, null);
  const missing = fallback === undefined ? false : fallback;
  return {
    read() {
      const raw = pref.read();
      if (raw === null) return missing;
      return raw === '1' || raw === 'true';
    },
    write(on) { pref.write(on ? '1' : '0'); },
  };
}
