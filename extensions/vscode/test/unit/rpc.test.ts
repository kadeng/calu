import { describe, expect, it } from 'vitest';
import { LineSplitter, RpcClient, RpcClosedError, RpcRequestError, type RpcEvent } from '../../src/rpc';

/**
 * Create a client whose written lines and received events are recorded.
 *
 * @returns The client and the recording arrays.
 */
function setup() {
  const written: string[] = [];
  const events: RpcEvent[] = [];
  const errors: string[] = [];
  const client = new RpcClient(
    (line) => written.push(line),
    (e) => events.push(e),
    (m) => errors.push(m),
  );
  return { client, written, events, errors };
}

describe('LineSplitter', () => {
  it('returns complete lines and buffers the rest', () => {
    const s = new LineSplitter();
    expect(s.push('a\nb')).toEqual(['a']);
    expect(s.push('c\r\n\nd')).toEqual(['bc', '']);
    expect(s.flush()).toBe('d');
    expect(s.flush()).toBe('');
  });
});

describe('RpcClient', () => {
  it('writes one NDJSON line per request with increasing ids', () => {
    const { client, written } = setup();
    void client.request('initialize', { client: 't' });
    void client.request('chat.send', { text: 'hi' });
    expect(written).toHaveLength(2);
    expect(written.every((l) => l.endsWith('\n') && !l.slice(0, -1).includes('\n'))).toBe(true);
    expect(JSON.parse(written[0])).toEqual({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { client: 't' } });
    expect(JSON.parse(written[1]).id).toBe(2);
  });

  it('resolves responses split across chunks, in any order', async () => {
    const { client } = setup();
    const a = client.request('a');
    const b = client.request('b');
    client.feed('{"jsonrpc":"2.0","id":2,"res');
    client.feed('ult":{"x":2}}\n{"jsonrpc":"2.0","id":1,"result":{"x":1}}\n');
    await expect(a).resolves.toEqual({ x: 1 });
    await expect(b).resolves.toEqual({ x: 2 });
    expect(client.pendingCount).toBe(0);
  });

  it('rejects with RpcRequestError for error answers', async () => {
    const { client } = setup();
    const p = client.request('chat.send', { text: 'x' });
    client.feed('{"jsonrpc":"2.0","id":1,"error":{"code":-32000,"message":"no session"}}\n');
    const error = await p.catch((e: unknown) => e);
    expect(error).toBeInstanceOf(RpcRequestError);
    expect((error as RpcRequestError).code).toBe(-32000);
    expect((error as Error).message).toContain('no session');
  });

  it('delivers notifications in order and reports garbage lines', () => {
    const { client, events, errors } = setup();
    client.feed('{"jsonrpc":"2.0","method":"log","params":{"message":"m"}}\nnot json\n');
    client.feed('{"jsonrpc":"2.0","method":"session.state"}\n');
    expect(events).toEqual([
      { method: 'log', params: { message: 'm' } },
      { method: 'session.state', params: {} },
    ]);
    expect(errors).toHaveLength(1);
  });

  it('rejects pending and later requests after close', async () => {
    const { client } = setup();
    const p = client.request('session.start');
    client.close('bridge exited with code 1');
    await expect(p).rejects.toBeInstanceOf(RpcClosedError);
    await expect(p).rejects.toThrow('bridge exited with code 1');
    await expect(client.request('x')).rejects.toBeInstanceOf(RpcClosedError);
    expect(client.closed).toBe(true);
  });

  it('processes a final unterminated line on close', async () => {
    const { client } = setup();
    const p = client.request('a');
    client.feed('{"jsonrpc":"2.0","id":1,"result":7}');
    client.close('eof');
    await expect(p).resolves.toBe(7);
  });

  it('rejects when write throws', async () => {
    const client = new RpcClient(
      () => {
        throw new Error('EPIPE');
      },
      () => {},
    );
    await expect(client.request('a')).rejects.toThrow('EPIPE');
    expect(client.pendingCount).toBe(0);
  });
});
