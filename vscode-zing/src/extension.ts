import * as vscode from 'vscode';
import { spawn } from 'child_process';
import * as path from 'path';

let output: vscode.OutputChannel;

/** Matches "file.zng:LINE:COL: message" produced by the zing CLI. */
const ERROR_RE = /^(.*\.zng):(\d+):(\d+):\s*(.*)$/;

function exePath(): string {
    return vscode.workspace
        .getConfiguration('zing')
        .get('path', 'zing');
}

/**
 * Run the CLI and buffer everything.
 *
 * Buffering means the output channel shows the whole program output even if
 * the user scrolled or switched tabs mid-run, which matters for short zing
 * programs that finish before you can read the live stream.
 */
function runCli(
    args: string[],
    cwd: string,
    onLine?: (line: string) => void,
    input?: string,
): Promise<{ code: number | null; stdout: string; stderr: string }> {
    return new Promise((resolve) => {
        const child = spawn(exePath(), args, { cwd });
        let stdout = '';
        let stderr = '';

        if (input !== undefined) {
            child.stdin?.end(input);
        }

        child.stdout.on('data', (d: Buffer) => {
            const s = d.toString();
            stdout += s;
            if (onLine) s.split(/\r?\n/).forEach(onLine);
        });

        child.stderr.on('data', (d: Buffer) => {
            const s = d.toString();
            stderr += s;
            if (onLine) s.split(/\r?\n/).forEach(onLine);
        });

        child.on('error', (err: NodeJS.ErrnoException) => {
            const hint = err.code === 'ENOENT'
                ? `Could not run '${exePath()}'. Install zing, or set ` +
                  `zing.path in settings.\n` +
                  `  Linux/macOS: curl -fsSL https://jaivardhanpandey66-create.github.io/zing/install.sh | sh\n` +
                  `  Windows: download the installer from the zing site`
                : err.message;
            output.appendLine(hint);
            resolve({ code: -1, stdout, stderr: hint });
        });

        child.on('close', (code) => resolve({ code, stdout, stderr }));
    });
}

function activeZingFile(): vscode.TextDocument | undefined {
    const editor = vscode.window.activeTextEditor;
    if (!editor || editor.document.languageId !== 'zing') {
        void vscode.window.showWarningMessage('zing: open a .zng file first');
        return undefined;
    }
    return editor.document;
}

/** Clear and show the output panel, focused. */
function showOutput(): void {
    output.show(true);
}

function banner(text: string): void {
    output.appendLine('');
    output.appendLine(text);
}

export function activate(context: vscode.ExtensionContext): void {
    output = vscode.window.createOutputChannel('zing');
    context.subscriptions.push(output);

    context.subscriptions.push(
        vscode.commands.registerCommand('zing.run', runFile),
        vscode.commands.registerCommand('zing.build', buildFile),
        vscode.commands.registerCommand('zing.check', checkFile),
        vscode.commands.registerCommand('zing.format', formatFile),
        vscode.commands.registerCommand('zing.showOutput', showOutput),
    );

    // Re-check on save so a broken file is underlined before it is run.
    context.subscriptions.push(vscode.workspace.onDidSaveTextDocument((doc) => {
        if (doc.languageId !== 'zing') return;
        if (!vscode.workspace.getConfiguration('zing').get('runOnSaveCheck', true)) {
            return;
        }
        void checkDocument(doc, false);
    }));
}

export function deactivate(): void {
    // Nothing to tear down: every run is a short-lived child process.
}

async function runFile(): Promise<void> {
    const doc = activeZingFile();
    if (!doc) return;
    const file = doc.uri.fsPath;
    const folder = path.dirname(file);

    output.clear();
    showOutput();
    banner(`$ zing run ${path.basename(file)}`);
    const t0 = Date.now();
    const res = await runCli(['run', file], folder, (line) => {
        if (line.length > 0) output.appendLine(line);
    });
    const ms = Date.now() - t0;
    if (res.code !== 0) {
        banner(`exited with code ${res.code} in ${ms} ms`);
    } else {
        banner(`finished in ${ms} ms`);
    }
    await reportProblems(doc, res.stderr, true);
}

async function buildFile(): Promise<void> {
    const doc = activeZingFile();
    if (!doc) return;
    output.clear();
    showOutput();
    banner(`$ zing build ${path.basename(doc.uri.fsPath)}`);
    const res = await runCli(
        ['build', doc.uri.fsPath], path.dirname(doc.uri.fsPath));
    if (res.stdout.trim()) output.appendLine(res.stdout.trim());
    if (res.stderr.trim()) output.appendLine(res.stderr.trim());
    if (res.code === 0) {
        banner('built a native binary next to the source');
    }
}

async function checkFile(): Promise<void> {
    const doc = activeZingFile();
    if (!doc) return;
    output.clear();
    showOutput();
    banner(`$ zing check ${path.basename(doc.uri.fsPath)}`);
    const res = await runCli(
        ['check', doc.uri.fsPath], path.dirname(doc.uri.fsPath));
    if (res.stdout.trim()) output.appendLine(res.stdout.trim());
    await reportProblems(doc, res.stderr, true);
}

async function checkDocument(
    doc: vscode.TextDocument, showPanel: boolean): Promise<void> {
    const res = await runCli(
        ['check', doc.uri.fsPath], path.dirname(doc.uri.fsPath));
    await reportProblems(doc, res.stderr, showPanel);
}

async function formatFile(): Promise<void> {
    const doc = activeZingFile();
    if (!doc) return;
    const res = await runCli(
        ['fmt', '-'], path.dirname(doc.uri.fsPath), undefined, doc.getText());
    if (res.code === 0 && res.stdout) {
        const edit = new vscode.WorkspaceEdit();
        edit.replace(
            doc.uri,
            new vscode.Range(
                doc.positionAt(0), doc.positionAt(doc.getText().length)),
            res.stdout,
        );
        await vscode.workspace.applyEdit(edit);
    } else if (res.stderr) {
        output.appendLine(res.stderr);
    }
}

/**
 * Turn CLI error lines into red squiggles.
 *
 * The CLI is the single source of truth for what is wrong with a program, so
 * the extension never re-implements any checking of its own.
 */
async function reportProblems(
    doc: vscode.TextDocument, stderr: string, showPanel: boolean,
): Promise<void> {
    const show = vscode.workspace
        .getConfiguration('zing').get('showInlineErrors', true);
    if (!stderr.trim() || !show) return;

    const diagnostics: vscode.Diagnostic[] = [];
    for (const line of stderr.split(/\r?\n/)) {
        const m = ERROR_RE.exec(line.trim());
        if (!m) continue;
        const [, , lineStr, colStr, message] = m;
        const lineNo = Math.max(0, parseInt(lineStr, 10) - 1);
        const colNo = Math.max(0, parseInt(colStr, 10) - 1);
        const range = safeRange(doc, lineNo, colNo);
        const d = new vscode.Diagnostic(
            range, message, vscode.DiagnosticSeverity.Error);
        d.source = 'zing';
        diagnostics.push(d);
    }

    if (!diagnostics.length) return;

    const collection = vscode.languages.createDiagnosticCollection('zing');
    collection.set(doc.uri, diagnostics);
    if (showPanel) output.appendLine(stderr.trim());

    // Hold the collection for a moment so the editor can render it, then drop
    // it; otherwise stale errors would stick around after a fix.
    setTimeout(() => collection.dispose(), 30000);
}

function safeRange(
    doc: vscode.TextDocument, line: number, col: number): vscode.Range {
    const l = Math.min(line, doc.lineCount - 1);
    const lineLen = doc.lineAt(l).text.length;
    const c = Math.min(col, lineLen);
    return new vscode.Range(l, c, l, Math.min(c + 1, lineLen));
}
