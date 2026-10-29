import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";

test("真实读取与导出链路正确呈现列表内化学式及后续列表和粗体", async ({ page, request }) => {
  expect((await (await request.get("/__test__/identity")).json()).fake_services_used).toBe(true);
  const upload = await request.post("/api/v1/videos", { multipart: { file: {
    name: "e2e-chemistry-list.mp4", mimeType: "video/mp4",
    buffer: Buffer.concat([readFileSync(process.env.E2E_VIDEO_PATH!), Buffer.from(test.info().testId)]),
  } } });
  expect(upload.status()).toBe(201);
  const video = await upload.json();
  const task = await (await request.post(`/api/v1/videos/${video.id}/analyses`)).json();
  await expect.poll(async () => (await (await request.get(`/api/v1/tasks/${task.task_id}`)).json()).status,
    { timeout: 15000 }).toBe("succeeded");
  const notes = await (await request.get(`/api/v1/analyses/${task.task_id}`)).json();
  expect(notes.chapters[0].markdown).toContain("    $$\n    \\ce{Ag+ + SCN- -> AgSCN v}");
  await page.goto(`/#summary/${video.id}`);
  const body = page.locator(".note-markdown");
  await expect(body.locator(".katex")).toHaveCount(4);
  await expect(body.locator(".katex-display")).toHaveCount(4);
  await expect(body.locator(".katex-error")).toHaveCount(0);
  await expect(body.locator("ol > li")).toHaveCount(2);
  await expect(body.locator("ul > li")).toHaveCount(2);
  await expect(body.locator("strong").last()).toHaveText("滴定条件：必须充分振摇");
  await expect(body.locator("code")).toHaveText(String.raw`\ce{Ag+}`);
  await expect(body).toContainText("普通金额 $20");
  await expect(body).not.toContainText("$$");
  // KaTeX 的隐藏 MathML annotation 会保留源码，检查公式外的正文是否泄漏命令。
  const prose = await body.evaluate(element => {
    const copy = element.cloneNode(true) as HTMLElement;
    copy.querySelectorAll(".katex, code").forEach(node => node.remove());
    return copy.textContent;
  });
  expect(prose).not.toContain(String.raw`\quad`);
  expect(prose).not.toContain(String.raw`\ce{`);
  const md = await request.get(`/api/v1/analyses/${task.task_id}/notes?format=md`);
  const txt = await request.get(`/api/v1/analyses/${task.task_id}/notes?format=txt`);
  expect(await md.text()).toContain(notes.chapters[0].markdown);
  expect(await txt.text()).toContain(String.raw`\ce{Ag+ + SCN- -> AgSCN v}`);
  await page.screenshot({ path: test.info().outputPath("chemistry-lists.png"), fullPage: true });
});
