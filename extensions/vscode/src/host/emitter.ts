/**
 * A minimal event emitter for the shared host code (`src/host/`), which runs in the VSCode
 * extension host and in a browser tab and so cannot use `vscode.EventEmitter`.
 *
 *     const changed = new Emitter<number>();
 *     const sub = changed.event((n) => console.log(n));   // subscribe
 *     changed.fire(3);                                     // listeners run synchronously
 *     sub.dispose();                                       // unsubscribe
 *
 * `Listen<T>` has the shape of `vscode.Event<T>` for the one-argument call, so a VSCode event
 * can be passed where a `Listen<T>` is expected and the other way round.
 */

/** Something that can be released. */
export interface Disposable {
  /** Release it; calling it again does nothing. */
  dispose(): void;
}

/**
 * Subscribe to an event.
 *
 * @param listener Called with each fired value, synchronously, in subscription order.
 * @returns The subscription; `dispose()` ends it.
 */
export type Listen<T> = (listener: (value: T) => unknown) => Disposable;

/** Fires values to its listeners. */
export class Emitter<T> {
  private listeners: ((value: T) => unknown)[] = [];

  /** Subscribe to the emitter (see `Listen`). */
  readonly event: Listen<T> = (listener) => {
    this.listeners.push(listener);
    return {
      dispose: () => {
        this.listeners = this.listeners.filter((l) => l !== listener);
      },
    };
  };

  /**
   * Call every listener with a value. A listener that throws does not stop the others; its
   * error is swallowed (listeners report their own failures).
   *
   * @param value The value.
   * @returns Nothing.
   */
  fire(value: T): void {
    for (const listener of [...this.listeners]) {
      try {
        listener(value);
      } catch {
        // A listener's failure is its own.
      }
    }
  }

  /**
   * Remove every listener.
   *
   * @returns Nothing.
   */
  dispose(): void {
    this.listeners = [];
  }
}
