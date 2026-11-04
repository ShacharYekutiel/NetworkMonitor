import type { WsMessage } from './types';

export type WsState = 'connecting' | 'open' | 'closed';
type Listener = (msg: WsMessage) => void;
type StateListener = (state: WsState) => void;

/** Reconnect backoff per api.md: 1 s, 2 s, 5 s, then 10 s max. */
export const BACKOFF_MS = [1000, 2000, 5000, 10000];

export function backoffDelay(attempt: number): number {
  return BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)];
}

export function wsUrl(loc: Location = window.location): string {
  const proto = loc.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${loc.host}/ws`;
}

export type LiveSocketOptions = {
  onUnauthorized?: () => void;
  onSetupRequired?: () => void;
  /** Called when a connection opens after a previous one dropped. */
  onReconnect?: () => void;
  socketFactory?: (url: string) => WebSocket;
};

/** The single shared WebSocket connection with auto-reconnect. */
export class LiveSocket {
  private ws: WebSocket | null = null;
  private listeners = new Set<Listener>();
  private stateListeners = new Set<StateListener>();
  private attempt = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private pingTimer: ReturnType<typeof setInterval> | null = null;
  private stopped = true;
  private hadOpen = false;
  private _state: WsState = 'closed';

  constructor(private readonly opts: LiveSocketOptions = {}) {}

  get state(): WsState {
    return this._state;
  }

  private setState(s: WsState) {
    this._state = s;
    this.stateListeners.forEach((l) => l(s));
  }

  subscribe(l: Listener): () => void {
    this.listeners.add(l);
    return () => this.listeners.delete(l);
  }

  onState(l: StateListener): () => void {
    this.stateListeners.add(l);
    return () => this.stateListeners.delete(l);
  }

  start(): void {
    if (!this.stopped) return;
    this.stopped = false;
    this.connect();
  }

  stop(): void {
    this.stopped = true;
    this.hadOpen = false;
    this.attempt = 0;
    if (this.timer) clearTimeout(this.timer);
    if (this.pingTimer) clearInterval(this.pingTimer);
    this.timer = null;
    this.pingTimer = null;
    const ws = this.ws;
    this.ws = null;
    if (ws) {
      ws.onclose = null;
      ws.close(1000);
    }
    this.setState('closed');
  }

  private connect() {
    if (this.stopped) return;
    this.setState('connecting');
    let ws: WebSocket;
    try {
      ws = this.opts.socketFactory ? this.opts.socketFactory(wsUrl()) : new WebSocket(wsUrl());
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.ws = ws;
    ws.onopen = () => {
      this.attempt = 0;
      this.setState('open');
      if (this.hadOpen) this.opts.onReconnect?.();
      this.hadOpen = true;
      if (this.pingTimer) clearInterval(this.pingTimer);
      this.pingTimer = setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'ping' }));
      }, 25000);
    };
    ws.onmessage = (ev: MessageEvent) => {
      let msg: WsMessage;
      try {
        msg = JSON.parse(String(ev.data)) as WsMessage;
      } catch {
        return;
      }
      if (!msg || typeof msg.type !== 'string') return;
      this.listeners.forEach((l) => l(msg));
    };
    ws.onclose = (ev: CloseEvent) => {
      if (this.pingTimer) clearInterval(this.pingTimer);
      this.pingTimer = null;
      this.ws = null;
      if (ev.code === 4401) {
        this.stopped = true;
        this.setState('closed');
        this.opts.onUnauthorized?.();
        return;
      }
      if (ev.code === 4403) {
        this.stopped = true;
        this.setState('closed');
        this.opts.onSetupRequired?.();
        return;
      }
      this.setState('closed');
      this.scheduleReconnect();
    };
  }

  private scheduleReconnect() {
    if (this.stopped) return;
    const delay = backoffDelay(this.attempt);
    this.attempt += 1;
    this.timer = setTimeout(() => this.connect(), delay);
  }
}
