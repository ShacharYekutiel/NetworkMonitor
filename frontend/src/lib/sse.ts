// [3b] Server-Sent Events parsing for fetch + ReadableStream (EventSource cannot POST). Pure.

export type RawSseEvent = { event: string; data: string; id: string | null };

/**
 * Incremental parser of a `text/event-stream` body (WHATWG rules: fields `event`, `data`, `id`;
 * lines starting with `:` are comments such as `: keepalive`; an empty line dispatches; CRLF, CR
 * and LF line ends). Feed it decoded text chunks of any size.
 */
export class SseParser {
  private buf = '';
  private event = '';
  private data: string[] = [];
  private id: string | null = null;
  private hasData = false;

  /** Returns the events completed by this chunk. */
  push(chunk: string): RawSseEvent[] {
    this.buf += chunk;
    const out: RawSseEvent[] = [];
    for (;;) {
      const m = /\r\n|\r|\n/.exec(this.buf);
      if (!m) break;
      // A lone CR at the very end may be the first half of CRLF: wait for more input.
      if (m[0] === '\r' && m.index === this.buf.length - 1) break;
      const line = this.buf.slice(0, m.index);
      this.buf = this.buf.slice(m.index + m[0].length);
      const ev = this.line(line);
      if (ev) out.push(ev);
    }
    return out;
  }

  /** End of stream: a trailing event without its blank line is dropped (per spec). */
  end(): RawSseEvent[] {
    this.buf = '';
    this.reset();
    return [];
  }

  private reset() {
    this.event = '';
    this.data = [];
    this.hasData = false;
  }

  private line(line: string): RawSseEvent | null {
    if (line === '') {
      if (!this.hasData) {
        this.reset();
        return null;
      }
      const ev = { event: this.event || 'message', data: this.data.join('\n'), id: this.id };
      this.reset();
      return ev;
    }
    if (line.startsWith(':')) return null;
    const i = line.indexOf(':');
    const field = i < 0 ? line : line.slice(0, i);
    let value = i < 0 ? '' : line.slice(i + 1);
    if (value.startsWith(' ')) value = value.slice(1);
    switch (field) {
      case 'event':
        this.event = value;
        break;
      case 'data':
        this.data.push(value);
        this.hasData = true;
        break;
      case 'id':
        if (!value.includes('\0')) this.id = value;
        break;
      default:
        break; // retry and unknown fields are ignored
    }
    return null;
  }
}
