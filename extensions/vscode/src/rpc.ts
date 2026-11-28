/**
 * JSON-RPC 2.0 client over NDJSON (one JSON object per line), as spoken by `calu ide`.
 *
 * This module has no `vscode` and no `child_process` dependency, so it can be unit-tested. The
 * caller connects it to a transport:
 *
 *     extension --write(line)--> bridge stdin
 *     bridge stdout --chunks--> feed(chunk) --> responses resolve request() promises
 *                                         --> notifications go to the onEvent listener
 *     process exit --> close(reason) --> all pending requests reject with RpcClosedError
 */

/** An error answer of the bridge (`{"error": {...}}`) for one request. */
export class RpcRequestError extends Error {
  /** JSON-RPC error code, e.g. -32602 for invalid params, -32000 for a failed request. */
  readonly code: number;
  /** Optional `data` member of the error (e.g. validation details). */
  readonly data: unknown;

  /**
   * Create the error from the `error` member of a JSON-RPC response.
   *
   * @param method The request method that failed (only used in the message).
   * @param error The `error` object; missing `code`/`message` default to -32603 / "unknown error".
   */
  constructor(method: string, error: { code?: number; message?: string; data?: unknown }) {
    super(`${method}: ${error.message ?? 'unknown error'} (code ${error.code ?? -32603})`);
    this.name = 'RpcRequestError';
    this.code = error.code ?? -32603;
    this.data = error.data;
  }
}

/** The connection was closed (bridge exited) before a response arrived, or after close(). */
export class RpcClosedError extends Error {
  /**
   * @param reason Human-readable reason, e.g. "bridge exited with code 1".
   */
  constructor(reason: string) {
    super(reason);
    this.name = 'RpcClosedError';
  }
}

/** A notification from the bridge: `{"method": ..., "params": ...}` without an id. */
export interface RpcEvent {
  method: string;
  params: Record<string, unknown>;
}

/**
 * Splits a stream of text chunks into complete lines.
 *
 * Invariant: `push` returns each complete line exactly once, in order, without the "\n" (and
 * without a trailing "\r"); an incomplete last line stays buffered until its "\n" arrives.
 */
export class LineSplitter {
  private buffer = '';

  /**
   * Append a chunk and return the lines it completed.
   *
   * @param chunk Any piece of the stream (may contain zero, one or many newlines).
   * @returns The completed lines, possibly empty. Blank lines are returned as "".
   */
  push(chunk: string): string[] {
    this.buffer += chunk;
    const parts = this.buffer.split('\n');
    this.buffer = parts.pop() ?? '';
    return parts.map((line) => (line.endsWith('\r') ? line.slice(0, -1) : line));
  }

  /**
   * Return and clear the buffered incomplete line (used at end of stream).
   *
   * @returns The rest of the buffer; "" if nothing is buffered.
   */
  flush(): string {
    const rest = this.buffer;
    this.buffer = '';
    return rest;
  }
}

interface Pending {
  method: string;
  resolve: (result: unknown) => void;
  reject: (error: Error) => void;
}

/**
 * Client side of the protocol. Create it with a `write` function, then call `feed` with every
 * chunk of the bridge's stdout and `close` when the bridge exits.
 */
export class RpcClient {
  private nextId = 1;
  private readonly pending = new Map<number, Pending>();
  private readonly splitter = new LineSplitter();
  private closedReason: string | undefined;

  /**
   * @param write Sends one complete line (the "\n" is included) to the bridge's stdin. It may
   *   throw; the error then rejects the request that tried to send.
   * @param onEvent Called synchronously, in arrival order, for each notification.
   * @param onProtocolError Called for lines that are not valid JSON-RPC (e.g. stray prints);
   *   such lines are otherwise ignored.
   */
  constructor(
    private readonly write: (line: string) => void,
    private readonly onEvent: (event: RpcEvent) => void,
    private readonly onProtocolError: (message: string) => void = () => {},
  ) {}

  /** True once `close` was called. No requests can be sent afterwards. */
  get closed(): boolean {
    return this.closedReason !== undefined;
  }

  /** Number of requests still waiting for a response (for tests and diagnostics). */
  get pendingCount(): number {
    return this.pending.size;
  }

  /**
   * Send a request and wait for its response.
   *
   * @param method The JSON-RPC method, e.g. "chat.send".
   * @param params The params object (sent as `{}` when omitted).
   * @returns A promise of the response's `result` member.
   * @throws (rejects) RpcRequestError if the bridge answers with an error; RpcClosedError if the
   *   connection is closed or closes before the answer arrives; any error thrown by `write`.
   */
  request<T = unknown>(method: string, params: object = {}): Promise<T> {
    if (this.closedReason !== undefined) {
      return Promise.reject(new RpcClosedError(this.closedReason));
    }
    const id = this.nextId++;
    return new Promise<T>((resolve, reject) => {
      this.pending.set(id, { method, resolve: resolve as (r: unknown) => void, reject });
      try {
        this.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
      } catch (error) {
        this.pending.delete(id);
        reject(error instanceof Error ? error : new Error(String(error)));
      }
    });
  }

  /**
   * Process a chunk of the bridge's stdout: resolve/reject requests and dispatch notifications
   * for every complete line in it.
   *
   * @param chunk Text received from stdout (any split; partial lines are buffered).
   * @returns Nothing. Exceptions thrown by `onEvent` propagate to the caller after the line
   *   that caused them; later lines of the same chunk are then discarded and never handled.
   */
  feed(chunk: string): void {
    for (const line of this.splitter.push(chunk)) {
      this.handleLine(line);
    }
  }

  /**
   * Mark the connection as closed and reject all pending requests with RpcClosedError.
   * Idempotent: later calls do nothing.
   *
   * @param reason Text for the rejection errors, e.g. "bridge exited with code 1".
   * @returns Nothing.
   */
  close(reason: string): void {
    if (this.closedReason !== undefined) return;
    const rest = this.splitter.flush();
    if (rest.trim()) this.handleLine(rest);
    this.closedReason = reason;
    const waiting = [...this.pending.values()];
    this.pending.clear();
    for (const p of waiting) p.reject(new RpcClosedError(`${p.method}: ${reason}`));
  }

  /**
   * Handle one complete line (see `feed`).
   *
   * @param line One line without newline.
   * @returns Nothing.
   */
  private handleLine(line: string): void {
    if (!line.trim()) return;
    let message: unknown;
    try {
      message = JSON.parse(line);
    } catch {
      this.onProtocolError(`not JSON: ${line.slice(0, 200)}`);
      return;
    }
    if (typeof message !== 'object' || message === null || Array.isArray(message)) {
      this.onProtocolError(`not a JSON object: ${line.slice(0, 200)}`);
      return;
    }
    const m = message as Record<string, unknown>;
    if (typeof m.id === 'number' && ('result' in m || 'error' in m)) {
      const p = this.pending.get(m.id);
      if (!p) {
        this.onProtocolError(`response for unknown id ${m.id}`);
        return;
      }
      this.pending.delete(m.id);
      if ('error' in m && m.error) {
        p.reject(new RpcRequestError(p.method, m.error as { code?: number; message?: string }));
      } else {
        p.resolve(m.result);
      }
    } else if (typeof m.method === 'string') {
      const params = (m.params ?? {}) as Record<string, unknown>;
      this.onEvent({ method: m.method, params });
    } else if ('error' in m) {
      // An error without a usable id, e.g. a parse error of a line we sent.
      this.onProtocolError(`error without id: ${line.slice(0, 200)}`);
    } else {
      this.onProtocolError(`unrecognised message: ${line.slice(0, 200)}`);
    }
  }
}
