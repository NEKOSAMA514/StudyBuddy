import { test, expect } from "@playwright/test";
import { readdirSync, readFileSync, existsSync } from "node:fs";
import { resolve, dirname, join } from "node:path";
import ts from "typescript";
import { noteCenterLink, noteLocationLink, readRoute } from "../src/navigation";

const sourceRoot = resolve("src");
function sourceFiles(folder = sourceRoot): string[] {
  return readdirSync(folder, { withFileTypes: true }).flatMap(item => item.isDirectory()
    ? sourceFiles(join(folder, item.name)) : /\.tsx?$/.test(item.name) ? [join(folder, item.name)] : []);
}
function syntax(file: string) {
  return ts.createSourceFile(file, readFileSync(file, "utf8"), ts.ScriptTarget.Latest, true, file.endsWith("tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
}
function imports(file: string) {
  const found: string[] = [];
  function visit(node: ts.Node) {
    if ((ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) && node.moduleSpecifier && ts.isStringLiteral(node.moduleSpecifier)) found.push(node.moduleSpecifier.text);
    if (ts.isCallExpression(node) && node.expression.kind === ts.SyntaxKind.ImportKeyword && node.arguments[0] && ts.isStringLiteral(node.arguments[0])) found.push(node.arguments[0].text);
    ts.forEachChild(node, visit);
  }
  visit(syntax(file));
  return found.filter(path => path.startsWith(".")).map(path => {
    const base = resolve(dirname(file), path);
    const target = [base, base + ".ts", base + ".tsx", join(base, "index.ts"), join(base, "index.tsx")].find(existsSync);
    expect(target, `${file}: ${path}`).toBeTruthy();
    return target!;
  });
}

test("前端类型、静态及动态导入无循环，契约由普通类型模块拥有", () => {
  const files = sourceFiles();
  const graph = new Map(files.map(file => [file, imports(file)]));
  const finished = new Set<string>();
  const visiting: string[] = [];
  function visit(file: string) {
    expect(visiting.includes(file), [...visiting, file].join(" → ")).toBe(false);
    if (finished.has(file)) return;
    visiting.push(file);
    for (const dependency of graph.get(file) || []) visit(dependency);
    visiting.pop(); finished.add(file);
  }
  files.forEach(visit);
  const contracts = new Set(["AnalysisSelection", "DocumentImportSelection", "RecognitionSelection", "RecognitionCatalog", "RecognitionModel", "BilibiliSession"]);
  for (const file of files) for (const node of syntax(file).statements) {
    if (ts.isImportDeclaration(node) && node.importClause?.namedBindings && ts.isNamedImports(node.importClause.namedBindings)) {
      const business = node.importClause.namedBindings.elements.some(item => contracts.has((item.propertyName || item.name).text));
      if (business) expect((node.moduleSpecifier as ts.StringLiteral).text).toMatch(/(?:^|\/)types$/);
    }
    if (ts.isTypeAliasDeclaration(node) && contracts.has(node.name.text)) expect(file).toBe(join(sourceRoot, "types.ts"));
  }
  for (const file of files) {
    const source = readFileSync(file, "utf8");
    if (/studybuddy-(error|success|info)/.test(source)) expect(file).toBe(join(sourceRoot, "notifications.ts"));
    if (source.includes("studybuddy-bilibili-session")) expect(file).toBe(join(sourceRoot, "downloads/bilibiliSession.ts"));
  }
});

for (const view of ["search", "marks"] as const) {
  test(`${view} 笔记链接往返保留任务、章节、小节和返回上下文`, () => {
    const returnTo = { query: "酶 & 抑制? +", page: 3, view };
    const target = { taskId: "saved/task?", chapterId: "C&2", headingIndex: 2, returnTo };
    expect(readRoute(noteLocationLink("document", target))).toMatchObject({ mode: "summary", fileId: "document", target });
    expect(readRoute(noteCenterLink(returnTo))).toMatchObject({ mode: "notes", ...returnTo });
    expect(readRoute(`#summary/document?task=saved&view=${view}&page=0&heading=-1`).target).toEqual({
      taskId: "saved", chapterId: undefined, headingIndex: undefined, returnTo: { query: "", page: 1, view },
    });
  });
}
