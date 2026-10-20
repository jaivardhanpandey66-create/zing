# zing for VS Code

Language support for [zing](https://github.com/jaivardhanpandey66-create/zing),
a small language for building things.

## Features

- **Syntax highlighting** for every token class the language has: keywords,
  strings, numbers, decorators, regex helpers, operators and punctuation.
- **Snippets** for classes, constructors, functions, loops, `match`, try/catch,
  list and map comprehensions, generators and decorators.
- **Run** the current file with <kbd>F5</kbd>.
- **Check** for errors with <kbd>Ctrl</kbd>+<kbd>Shift</kbd>+<kbd>B</kbd>,
  shown as red squiggles in the editor.
- **Build** a native binary with `zing build`, straight from the palette.
- **Format** the current file with `zing fmt`.

Diagnostics come from the `zing` CLI itself, so the editor never re-implements
any checking of its own: if the command line and the editor ever disagree, the
command line wins by construction.

## Requirements

The `zing` command must be on your `PATH`, or set `zing.path` in settings to
an absolute path. Install it with:

```bash
curl -fsSL https://jaivardhanpandey66-create.github.io/zing/install.sh | sh
```

## Commands

| Command | Default key | What it does |
| --- | --- | --- |
| `zing: Run File` | <kbd>F5</kbd> | Run the current `.zng` file |
| `zing: Check for Errors` | <kbd>Ctrl</kbd>+<kbd>Shift</kbd>+<kbd>B</kbd> | Parse and report problems |
| `zing: Build Native Binary` | | Compile to a native executable |
| `zing: Format File` | | Reformat the buffer |
| `zing: Show Output` | | Focus the output panel |

## Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `zing.path` | `zing` | Path to the executable |
| `zing.runOnSaveCheck` | `true` | Check for errors on save |
| `zing.showInlineErrors` | `true` | Show errors as squiggles |
| `zing.stopOnError` | `true` | Stop a run at the first error |

## Building from source

```bash
npm install
npm run compile
npx @vscode/vsce package
```

## Publishing

```bash
npx @vscode/vsce login zing-lang   # once, paste an Azure DevOps PAT
npx @vscode/vsce publish
```

The token needs the **Marketplace → Manage** scope, created under
*All accessible organizations*.

## License

MIT
