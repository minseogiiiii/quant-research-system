// Syntax-only transpilation, NOT a build or runtime test.
const ts = require('typescript');
const fs = require('fs');
let bad = 0;
for (const filename of ['src/datafeed.ts', 'src/main.tsx', 'vite.config.ts']) {
  const source = fs.readFileSync(filename, 'utf8');
  const result = ts.transpileModule(source, {
    fileName: filename,
    reportDiagnostics: true,
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022,
      jsx: ts.JsxEmit.ReactJSX },
  });
  const errors = (result.diagnostics ?? []).filter(d => d.category === ts.DiagnosticCategory.Error);
  if (errors.length) {
    errors.forEach(d => console.error(`${filename}: ${ts.flattenDiagnosticMessageText(d.messageText, '\n')}`));
    bad += errors.length;
  } else {
    console.log(`TS/TSX syntax OK: ${filename}`);
  }
}
process.exitCode = bad ? 1 : 0;
