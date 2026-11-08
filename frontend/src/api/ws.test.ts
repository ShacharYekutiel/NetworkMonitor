import { describe, expect, it } from 'vitest';
import { LiveSocket } from './ws';

class FakeWs {
  static OPEN = 1;
  readyState = 0;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onclose: ((e: { code: number }) => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  send(s: string) {
    this.sent.push(s);
  }
  close() {}
  open() {
    this.readyState = 1;
    this.onopen?.();
  }
}

describe('[2b-3] LiveSocket topics', () => {
  it('subscribes once per topic, unsubscribes after the last user and re-sends on open', () => {
    (globalThis as unknown as { WebSocket: unknown }).WebSocket ??= FakeWs;
    const sockets: FakeWs[] = [];
    const s = new LiveSocket({
      socketFactory: () => {
        const w = new FakeWs();
        sockets.push(w);
        return w as unknown as WebSocket;
      },
    });
    const a = s.subscribeTopic('syslog'); // before connect: remembered only
    s.start();
    sockets[0].open();
    expect(sockets[0].sent.map((x) => JSON.parse(x))).toEqual([
      { type: 'subscribe', topics: ['syslog'] },
    ]);
    const b = s.subscribeTopic('syslog');
    const c = s.subscribeTopic('trap');
    expect(sockets[0].sent.length).toBe(2);
    a();
    a(); // idempotent
    expect(sockets[0].sent.length).toBe(2);
    b();
    expect(JSON.parse(sockets[0].sent[2])).toEqual({ type: 'unsubscribe', topics: ['syslog'] });
    expect(s.activeTopics()).toEqual(['trap']);
    c();
    expect(s.activeTopics()).toEqual([]);
    s.stop();
  });
});
