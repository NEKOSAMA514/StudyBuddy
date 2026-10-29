import { expect, test } from "@playwright/test";
test.beforeEach(async ({ request }) => {
  const identity = await request.get("/__test__/identity");
  expect(identity.ok()).toBeTruthy();
  expect((await identity.json()).fake_services_used).toBe(true);
  await request.put("/api/v1/settings/model", { data: { token: "fake-document-import", stream: false } });
  await request.put("/api/v1/settings/note-style", { data: { note_style: "detailed" } });
  for (const document of await (await request.get("/api/v1/documents")).json()) {
    if (document.latest_task && ["queued", "running", "stopping"].includes(document.latest_task.status)) {
      await request.post(`/api/v1/document-tasks/${document.latest_task.id}/stop`);
      await expect.poll(async () => (await (await request.get(`/api/v1/document-tasks/${document.latest_task.id}`)).json()).status).toBe("paused");
    }
    expect((await request.delete(`/api/v1/documents/${document.id}`)).ok()).toBeTruthy();
  }
});

for (const extension of ["pdf", "docx", "pptx", "xlsx", "csv", "txt", "md"]) {
  test(`本地文档上传与来源预览 ${extension}`, async ({ page, request }) => {
    await page.goto("/#summary");
    const filename = `sample.${extension}`;
    const source = await request.get(`/__test__/document-samples/${extension}`);
    expect(source.ok()).toBeTruthy();
    await expect(page.getByLabel("选择本地文件", { exact: true })).toHaveAttribute("accept", new RegExp(`\\.${extension}`));
    await page.getByLabel("选择本地文件", { exact: true }).setInputFiles({ name: filename, mimeType: "application/octet-stream", buffer: await source.body() });
    const setup = page.getByRole("dialog", { name: "生成文档笔记" });
    await expect(page.getByRole("heading", { name: filename, exact: true })).toBeVisible();
    await expect(setup).toHaveCount(0);
    const pending = await (await request.get("/api/v1/documents")).json();
    expect(pending).toHaveLength(1);
    expect(pending[0].extracted).toBe(false);
    await page.getByRole("button", { name: "开始处理", exact: true }).click();
    await expect(setup).toBeVisible();
    if (extension === "pdf") await setup.getByLabel("PDF 解析方式").selectOption("local");
    if (extension === "txt") await setup.getByRole("combobox", { name: /^笔记风格/ }).selectOption("concise");
    await setup.getByRole("button", { name: "开始生成", exact: true }).click();
    await expect(page.getByRole("heading", { name: filename, exact: true })).toBeVisible();
    const preview = page.getByRole("region", { name: "文档内容预览" });
    await expect(preview.locator("details")).not.toHaveAttribute("open");
    await expect(preview.getByRole("spinbutton")).toBeHidden();
    await preview.locator("summary").click();
    await expect(preview.locator("article").first()).toBeVisible();
    const labels: Record<string, string> = { pdf: "第 1 页", docx: "正文块 1", pptx: "幻灯片 1", xlsx: "测量记录", csv: "第 1–2 条记录", txt: "第 1–2 行", md: "第 1–7 行" };
    await expect(preview.locator("h3").first()).toContainText(labels[extension]);
    await expect(page.getByRole("button", { name: "开始处理", exact: true })).toHaveCount(0);
    await expect(page.getByRole("link", { name: "下载原文件" })).toHaveCount(0);
    const id = new URL(page.url()).hash.split("/")[1];
    await expect.poll(async () => (await (await request.get("/api/v1/documents")).json()).find((doc: { id: string }) => doc.id === id)?.latest_task?.status).toBe("succeeded");
    const task = (await (await request.get("/api/v1/documents")).json()).find((doc: { id: string }) => doc.id === id).latest_task;
    expect(task.note_style).toBe(extension === "txt" ? "concise" : "detailed");
    expect((await (await request.get("/api/v1/settings/note-style")).json()).note_style).toBe("detailed");
    const response = await request.get(`/api/v1/documents/${id}/original`);
    expect(response.ok()).toBeTruthy();
    expect((await response.body()).length).toBeGreaterThan(0);
    if (extension === "md") {
      await expect(preview.locator("strong")).toContainText("重点");
      await expect(preview.locator("script, img")).toHaveCount(0);
    }
    if (extension === "xlsx") {
      await expect(preview.locator("td").filter({ hasText: "=B2*2" })).toBeVisible();
      await page.setViewportSize({ width: 598, height: 871 });
      await page.getByRole("button", { name: "切换到暗色模式" }).click();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
      await page.screenshot({ path: test.info().outputPath("document-table-dark.png"), fullPage: true, animations: "disabled" });
    }
    await page.reload();
    await expect(page.getByRole("heading", { name: filename, exact: true })).toBeVisible();
    await request.delete(`/api/v1/documents/${id}`);
  });
}

test("文档定位、管理和窄屏显示", async ({ page, request }, testInfo) => {
  const filename = `定位-${testInfo.project.name}.txt`;
  const response = await request.post("/api/v1/documents", { multipart: { file: { name: filename, mimeType: "text/plain", buffer: Buffer.from(Array.from({ length: 1250 }, (_, i) => `内容第${i + 1}行`).join("\n")) } } });
  const document = await response.json();
  await page.setViewportSize({ width: 598, height: 871 });
  await page.goto(`/#summary/${document.id}`);
  await page.locator(".document-preview summary").click();
  await page.getByRole("spinbutton", { name: "内容块" }).fill("12");
  await page.getByRole("button", { name: "定位", exact: true }).click();
  await expect(page.locator(".document-unit h3").first()).toContainText("第 1101–1200 行");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
  await page.screenshot({ path: testInfo.outputPath("document-mobile.png"), fullPage: true, animations: "disabled" });
  await page.getByRole("button", { name: "← 返回", exact: true }).click();
  await page.getByRole("button", { name: `管理文件：${filename}`, exact: true }).last().click();
  await page.getByRole("button", { name: "重命名", exact: true }).click();
  await page.getByRole("textbox", { name: "文件名称" }).fill("改名后的文档");
  await page.getByRole("button", { name: "保存名称", exact: true }).click();
  await expect(page.getByRole("button", { name: "改名后的文档", exact: true }).last()).toBeVisible();
  await page.getByRole("button", { name: "管理文件：改名后的文档", exact: true }).last().click();
  await page.getByRole("button", { name: "删除", exact: true }).click();
  await page.getByRole("button", { name: "确认删除", exact: true }).click();
  await expect(page.getByRole("button", { name: "改名后的文档", exact: true })).toHaveCount(0);
});

test("切换文档时忽略迟到内容", async ({ page, request }) => {
  const create = async (name: string) => (await request.post("/api/v1/documents", { multipart: { file: { name: `${name}.txt`, mimeType: "text/plain", buffer: Buffer.from(name) } } })).json();
  const first = await create("旧文档"), second = await create("新文档");
  let release!: () => void;
  const hold = new Promise<void>(resolve => { release = resolve; });
  await page.route(`**/documents/${first.id}/content?**`, async route => {
    await hold;
    await route.continue().catch(() => {});
  });
  await page.goto(`/#summary/${first.id}`);
  await expect(page.getByRole("heading", { name: "旧文档.txt", exact: true })).toBeVisible();
  await page.goto(`/#summary/${second.id}`);
  await page.locator(".document-preview summary").click();
  await expect(page.locator(".document-text")).toHaveText("新文档");
  release();
  await expect(page.locator(".document-text")).toHaveText("新文档");
  await request.delete(`/api/v1/documents/${first.id}`);
  await request.delete(`/api/v1/documents/${second.id}`);
});
