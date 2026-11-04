/** data-model.md § notification_channels (telegram). */
export const TELEGRAM_TOKEN_RE = /^\d{3,20}:[A-Za-z0-9_-]{20,100}$/;
export const TELEGRAM_CHAT_ID_RE = /^(-?\d{1,20}|@[A-Za-z][A-Za-z0-9_]{3,31})$/;

export function isTelegramToken(s: string): boolean {
  return TELEGRAM_TOKEN_RE.test(s);
}

export function isTelegramChatId(s: string): boolean {
  return TELEGRAM_CHAT_ID_RE.test(s);
}
