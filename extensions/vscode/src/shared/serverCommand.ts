/**
 * The command line that starts `calu serve` in a terminal so that it keeps running after the
 * editor closes (spec section 6). Pure: the settings and what is installed are passed in.
 *
 *     settings (calu.command, calu.server.sudo / insecure / extraArgs)
 *        │
 *        ▼
 *     serveArgv  ──►  tmux installed?   tmux new-session -A -s calu '<serve command>'   detach: Ctrl+B D
 *                     screen installed? screen -S calu -D -R <serve command>            detach: Ctrl+A D
 *                     neither           <serve command>            (ends with the terminal)
 *
 * The extension never runs this command as a child process: it may ask for a sudo password or a
 * credential password, which the user types into the terminal.
 */

/** Name of the tmux and screen session that holds the server. */
export const SERVER_SESSION_NAME = 'calu';

/** Inputs of `buildServerHelp`. */
export interface ServerCommandInput {
  /** The argv prefix that runs calu, e.g. `["/home/me/.local/bin/calu"]`; empty means `calu`. */
  calu: readonly string[];
  /** `calu.server.sudo`: add `--sudo`. */
  sudo: boolean;
  /** `calu.server.insecure`: add `--insecure`. */
  insecure: boolean;
  /** `calu.server.extraArgs`: appended as they are (each one argument). */
  extraArgs: readonly string[];
  /** True if `tmux` is installed on the workspace machine. */
  tmux: boolean;
  /** True if `screen` is installed on the workspace machine. */
  screen: boolean;
}

/** What the user is shown about starting the server (also part of the chat view's state). */
export interface ServerHelp {
  /** The complete command for a POSIX shell. */
  command: string;
  /** What keeps the server running: "plain" means nothing does. */
  tool: 'tmux' | 'screen' | 'plain';
  /** How to leave the server running and get the terminal back; "" for "plain". */
  detach: string;
  /** One or two sentences on what the command does and what happens when the editor closes. */
  note: string;
}

/**
 * Quote one argument for a POSIX shell.
 *
 * @param arg Any string, including the empty string and strings with quotes or newlines.
 * @returns `arg` unchanged if it consists of characters that need no quoting, else the argument
 *   in single quotes with each embedded single quote written as `'\''`.
 *
 * @example shellQuote("it's") === "'it'\\''s'"
 */
export function shellQuote(arg: string): string {
  if (/^[A-Za-z0-9_@%+=:,./-]+$/.test(arg)) return arg;
  return `'${arg.replace(/'/g, `'\\''`)}'`;
}

/**
 * Join arguments to one shell command.
 *
 * @param argv The arguments.
 * @returns The arguments quoted with `shellQuote`, separated by spaces.
 */
export function shellJoin(argv: readonly string[]): string {
  return argv.map(shellQuote).join(' ');
}

/**
 * Build the argv of `calu serve` from the settings.
 *
 * @param input The settings (the `tmux` and `screen` fields are not used).
 * @returns `[...calu, "serve", ("--sudo")?, ("--insecure")?, ...extraArgs]`; empty strings in
 *   `calu` and `extraArgs` are dropped, and an empty `calu` stands for `["calu"]`.
 */
export function serveArgv(input: Pick<ServerCommandInput, 'calu' | 'sudo' | 'insecure' | 'extraArgs'>): string[] {
  const calu = input.calu.filter((part) => part !== '');
  const argv = [...(calu.length > 0 ? calu : ['calu']), 'serve'];
  if (input.sudo) argv.push('--sudo');
  if (input.insecure) argv.push('--insecure');
  argv.push(...input.extraArgs.filter((part) => part !== ''));
  return argv;
}

/**
 * Build the server command and the texts that explain it.
 *
 * @param input The settings and what is installed.
 * @returns The command for a POSIX shell with its tool, detach hint and note. With tmux, the
 *   serve command is one quoted argument (tmux passes it to a shell); with screen, its
 *   arguments follow screen's own.
 */
export function buildServerHelp(input: ServerCommandInput): ServerHelp {
  const argv = serveArgv(input);
  const serve = shellJoin(argv);
  if (input.tmux) {
    return {
      command: shellJoin(['tmux', 'new-session', '-A', '-s', SERVER_SESSION_NAME, serve]),
      tool: 'tmux',
      detach: 'Ctrl+B, then D',
      note:
        `Starts the calu server in the tmux session "${SERVER_SESSION_NAME}", or opens that session if it ` +
        'exists. Detach to leave it running; it keeps running after VSCode closes.',
    };
  }
  if (input.screen) {
    return {
      command: shellJoin(['screen', '-S', SERVER_SESSION_NAME, '-D', '-R', ...argv]),
      tool: 'screen',
      detach: 'Ctrl+A, then D',
      note:
        `Starts the calu server in the screen session "${SERVER_SESSION_NAME}", or opens that session if it ` +
        'exists. Detach to leave it running; it keeps running after VSCode closes.',
    };
  }
  return {
    command: serve,
    tool: 'plain',
    detach: '',
    note:
      'Starts the calu server in this terminal. The server ends when the terminal closes; install tmux ' +
      'or screen to keep it running after VSCode closes.',
  };
}

/**
 * Describe the root installation that `calu serve --sudo` needs, as a command to copy.
 *
 * The extension only shows this command; it needs sudo and is never run by the extension.
 *
 * @param source What to install (`calu.install.source`).
 * @returns The shell command of CALU_GUIDE.md that installs calu for root with uv (below
 *   /usr/local/lib/calu, linked from /usr/local/bin).
 */
export function rootInstallCommand(source: string): string {
  return (
    'sudo env UV_TOOL_DIR=/usr/local/lib/calu UV_TOOL_BIN_DIR=/usr/local/bin ' +
    `UV_PYTHON_INSTALL_DIR=/usr/local/lib/calu/python uv tool install ${shellQuote(source)}`
  );
}
