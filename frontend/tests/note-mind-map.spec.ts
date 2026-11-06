import { expect, test, type APIRequestContext, type Locator } from "@playwright/test";
import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";

async function version(request: APIRequestContext, kind: "video" | "document", fileId: string, style: string) {
  const response = await request.post(`/api/v1/${kind}s/${fileId}/analyses`, { data: { note_style: style } });
  expect(response.status()).toBe(202);
  const { task_id } = await response.json();
  const endpoint = kind === "video" ? "/api/v1/tasks" : "/api/v1/document-tasks";
  await expect.poll(async () => (await (await request.get(`${endpoint}/${task_id}`)).json()).status,
    { timeout: 20000 }).toBe("succeeded");
  return task_id as string;
}

async function loaded(image: Locator) {
  await expect(image).toBeVisible({ timeout: 12000 });
  await expect.poll(() => image.evaluate((element: HTMLImageElement) => element.naturalWidth)).toBeGreaterThan(1000);
}

for (const kind of ["video", "document"] as const) {
  test(`${kind} 笔记导图在后台生成并按版本预览和下载`, async ({ page, request }) => {
    expect((await (await request.get("/__test__/identity")).json()).fake_services_used).toBe(true);
    const suffix = randomUUID();
    const model = `e2e-mind-map-held-${suffix}`;
    const profilePath = `/__test__/mind-maps/profiles?model=${model}`;
    const profiles = await request.post(profilePath);
    expect(profiles.ok()).toBe(true);
    const { active, selected, invalid } = await profiles.json();
    const video = kind === "video";
    let id: string | undefined;
    try {
      const upload = await request.post(`/api/v1/${kind}s`, { multipart: { file: video ? {
        name: `mind-map-${suffix}.mp4`, mimeType: "video/mp4",
        buffer: Buffer.concat([readFileSync(process.env.E2E_VIDEO_PATH!), Buffer.from(suffix)]),
      } : { name: `mind-map-${suffix}.txt`, mimeType: "text/plain",
        buffer: Buffer.from(`学习笔记说明关键概念和适用条件。以下是例子和应当保留的限制。${suffix}`) } } });
      expect(upload.status()).toBe(201);
      id = (await upload.json()).id as string;
      const previous = await version(request, kind, id, "concise");
      const latest = await version(request, kind, id, "detailed");
      const root = `/api/v1/note-mind-maps/${kind}`;
      await page.goto(`/#summary/${id}`);
      const panel = page.locator(`#${kind}-notes`);
      const menu = panel.getByRole("button", { name: "笔记操作" });
      const openSetup = async () => {
        await menu.click();
        await page.getByRole("button", { name: "生成思维导图", exact: true }).click();
        return page.getByRole("dialog", { name: "生成思维导图", exact: true });
      };
      const setup = await openSetup();
      const picker = setup.getByRole("combobox", { name: "API 配置" });
      await expect(picker).toHaveValue(active);
      await expect(setup.getByRole("combobox")).toHaveCount(1);
      await picker.selectOption(selected);
      const sent = page.waitForRequest(req => req.method() === "POST" && req.url().endsWith(`${root}/${latest}`));
      await setup.getByRole("button", { name: "开始生成" }).click();
      expect((await sent).postDataJSON()).toEqual({ llm_profile_id: selected, replace_existing: false });
      await expect(setup).toBeHidden();
      const overviewMap = panel.locator(".overview > .note-mind-map");
      await expect(overviewMap.getByRole("status")).toHaveText("正在生成思维导图…");
      await page.reload();
      await expect(overviewMap.getByRole("status")).toHaveText("正在生成思维导图…");
      await panel.getByRole("button", { name: "上一份笔记" }).click();
      await expect(panel.locator(".note-title")).toBeVisible();
      await expect(panel.locator(".note-mind-map")).toHaveCount(0);
      await panel.getByRole("button", { name: "最新笔记" }).click();
      await expect(overviewMap.getByRole("status")).toHaveText("正在生成思维导图…");

      expect((await request.post(`/__test__/mind-maps/release?model=${model}`)).ok()).toBe(true);
      const thumbnail = overviewMap.getByRole("button", { name: "放大思维导图" });
      const small = thumbnail.getByRole("img");
      await loaded(small);
      const src = await small.getAttribute("src");
      expect(src).toContain(`${root}/${latest}/image?v=`);
      await expect(panel.locator(".overview > .summary-tags + .note-mind-map")).toBeVisible();
      if (video && test.info().project.name === "desktop-1280") {
        await overviewMap.scrollIntoViewIfNeeded();
        await page.screenshot({ path: test.info().outputPath("mind-map-desktop.png") });
      }

      await thumbnail.click();
      const preview = page.getByRole("dialog", { name: "思维导图", exact: true });
      const large = preview.getByRole("img");
      const expectFullScreen = async () => {
        await loaded(large);
        expect(await preview.boundingBox()).toEqual({ x: 0, y: 0, ...page.viewportSize()! });
        await expect(large).toHaveCSS("object-fit", "contain");
      };
      await expectFullScreen();
      expect(await large.evaluate(element => element.dispatchEvent(
        new MouseEvent("contextmenu", { button: 2, bubbles: true, cancelable: true })
      ))).toBe(true);
      const png = await request.get((await large.getAttribute("src"))!);
      expect(png.headers()["content-type"]).toBe("image/png");
      expect(png.headers()["content-disposition"]).toMatch(/^inline; .*\.png$/);
      expect((await png.body()).subarray(0, 8)).toEqual(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]));
      if (video && test.info().project.name === "desktop-1280") {
        await page.screenshot({ path: test.info().outputPath("mind-map-fullscreen-desktop.png") });
      }
      await preview.getByRole("button", { name: "关闭思维导图" }).click();
      await expect(preview).toBeHidden();

      const openRegenerate = async () => {
        await menu.click();
        await page.getByRole("button", { name: "重新生成思维导图", exact: true }).click();
        return page.getByRole("dialog", { name: "重新生成思维导图" });
      };
      const regenerate = await openRegenerate();
      await regenerate.getByRole("combobox").selectOption(invalid);
      await regenerate.getByRole("button", { name: "开始生成" }).click();
      await expect(overviewMap.getByRole("button", { name: "重试生成" })).toBeVisible();
      await expect(small).toHaveAttribute("src", src!);
      await page.reload();
      await expect(small).toHaveAttribute("src", src!);

      await panel.getByRole("button", { name: "上一份笔记" }).click();
      await (await openSetup()).getByRole("button", { name: "开始生成" }).click();
      const older = panel.locator(".note-title + .note-mind-map img");
      await loaded(older);
      await expect(older).toHaveAttribute("src", new RegExp(`${previous}/image`));
      const mapTitle = panel.getByRole("heading", { name: "思维导图", exact: true });
      const subheading = panel.locator(".note-markdown[data-density=body] h2").first();
      const mapSection = panel.locator(":scope > .note-mind-map");
      const body = panel.locator(":scope > .timeline");
      const typography = (element: Element) => {
        const style = getComputedStyle(element);
        return [style.fontFamily, style.fontSize, style.fontWeight, style.lineHeight, style.color];
      };
      const viewport = page.viewportSize()!;
      for (const width of [1920, viewport.width, 686, 375]) {
        await page.setViewportSize({ ...viewport, width });
        expect(await mapTitle.evaluate(typography)).toEqual(await subheading.evaluate(typography));
        const mapBox = (await mapSection.boundingBox())!;
        const bodyBox = (await body.boundingBox())!;
        expect([mapBox.x, mapBox.width]).toEqual([bodyBox.x, bodyBox.width]);
        if (width === 1920) expect(bodyBox.width).toBe(1280);
        if (video && test.info().project.name === "desktop-1280") {
          await mapTitle.evaluate(element => element.scrollIntoView({ block: "center" }));
          await page.screenshot({ path: test.info().outputPath(`mind-map-heading-${width}.png`), animations: "disabled" });
        }
      }
      await page.setViewportSize(viewport);
      await panel.getByRole("button", { name: "最新笔记" }).click();
      await expect(small).toHaveAttribute("src", src!);

      await page.setViewportSize({ width: 375, height: 800 });
      await menu.click();
      const view = page.getByRole("button", { name: "查看思维导图" });
      await expect(view).toBeInViewport();
      await view.click();
      await expectFullScreen();
      await expect(preview.getByRole("button")).toHaveCount(1);
      if (video && test.info().project.name === "desktop-1280") {
        await page.screenshot({ path: test.info().outputPath("mind-map-mobile.png") });
      }
      await page.keyboard.press("Escape");
      await expect(preview).toBeHidden();
      await openRegenerate();
      await expect(regenerate.getByRole("combobox")).toBeInViewport();
      await expect(regenerate.getByRole("button", { name: "开始生成" })).toBeInViewport();
      if (video && test.info().project.name === "desktop-1280") {
        await page.screenshot({ path: test.info().outputPath("mind-map-setup-mobile.png") });
      }
      await page.keyboard.press("Escape");
      await expect(regenerate).toBeHidden();
    } finally {
      await request.post(`/__test__/mind-maps/release?model=${model}`);
      if (id) await request.delete(`/api/v1/${kind}s/${id}`);
      await request.delete(profilePath);
    }
  });
}
