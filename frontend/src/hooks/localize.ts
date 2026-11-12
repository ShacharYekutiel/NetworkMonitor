import { useMemo, useSyncExternalStore } from 'react';
import { useTranslation } from 'react-i18next';
import { localizeMessage, namedValueMapsVersion, subscribeNamedValueMaps } from '../lib/valueMaps';

/**
 * [1.3.2] Returns a function that shows a server-language status / event message in the
 * viewer's language where it came from a named value map (lib/valueMaps `localizeMessage`).
 * Its identity changes with the UI language and when the maps (loaded once in Layout) arrive,
 * so every user re-renders then.
 */
export function useLocalizeMessage(): (message: string | null | undefined) => string {
  const { i18n } = useTranslation();
  const lang = i18n.language;
  const version = useSyncExternalStore(
    subscribeNamedValueMaps,
    namedValueMapsVersion,
    namedValueMapsVersion,
  );
  return useMemo(() => {
    void version; // a new function when the maps change
    return (message: string | null | undefined) => localizeMessage(message, lang);
  }, [lang, version]);
}
