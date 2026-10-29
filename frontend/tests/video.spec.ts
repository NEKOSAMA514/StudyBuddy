import { taskResponse } from "./support/taskContract";
import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";
import { basename, dirname, join } from "node:path";

const samplePath = process.env.E2E_VIDEO_PATH!;
const logPath = process.env.E2E_LOG_PATH!;
if (!samplePath || !logPath) throw new Error("请使用 scripts/run-tests.ps1 -Suite Browser。");
test.beforeAll(async ({ request }) => {
  const identity = await request.get("/__test__/identity");
  expect(identity.ok(), "只能针对隔离的测试工厂运行，不能使用正式服务").toBe(true);
  expect((await identity.json()).fake_services_used).toBe(true);
});

test("模型或运行引擎未准备好时禁止保存，可用OCR选择刷新后保留", async ({ page, request }) => {
  const catalog = await (await request.get("/api/v1/settings/recognition")).json();
  const asr = catalog.models.find((model: { id: string }) => model.id === "whisper-base");
  const ocr = catalog.models.find((model: { id: string }) => model.id === "rapidocr");
  asr.ready = false;
  asr.runtime_ready = true;
  ocr.ready = true;
  ocr.runtime_ready = false;
  const saves: unknown[] = [];
  await page.route("**/api/v1/settings/recognition", async route => {
    if (route.request().method() === "PUT") {
      catalog.settings = route.request().postDataJSON();
      saves.push(catalog.settings);
    }
    await route.fulfill({ json: catalog });
  });
  await page.goto("/#settings");
  const dialog = page.getByRole("dialog", { name: "设置", exact: true });
  await dialog.getByRole("tab", { name: "语音识别模型", exact: true }).click();
  await dialog.getByRole("combobox", { name: "语音识别模型", exact: true }).selectOption("whisper-base");
  await expect(dialog.getByRole("button", { name: "保存识别设置" })).toBeDisabled();
  await dialog.getByRole("tab", { name: "OCR 模型", exact: true }).click();
  await dialog.getByRole("combobox", { name: "OCR 模型", exact: true }).selectOption("rapidocr");
  await expect(dialog.getByRole("button", { name: "保存识别设置" })).toBeDisabled();
  expect(saves).toEqual([]);

  ocr.runtime_ready = true;
  await page.reload();
  await dialog.getByRole("tab", { name: "OCR 模型", exact: true }).click();
  await dialog.getByRole("combobox", { name: "OCR 模型", exact: true }).selectOption("rapidocr");
  const expected = { ...catalog.settings, ocr_option: "rapidocr" };
  await dialog.getByRole("button", { name: "保存识别设置" }).click();
  await expect(dialog.getByRole("status").filter({ hasText: "识别设置已保存" })).toBeVisible();
  expect(saves).toEqual([expected]);
  await page.reload();
  await dialog.getByRole("tab", { name: "OCR 模型", exact: true }).click();
  await expect(dialog.getByRole("combobox", { name: "OCR 模型", exact: true })).toHaveValue("rapidocr");
});

for (const identifier of ["rapidocr"]) {
  test(`OCR 卡片精简、打开文件夹与保存刷新：${identifier}`, async ({ page, request }, testInfo) => {
    const catalog = await (await request.get("/api/v1/settings/recognition")).json();
    const model = catalog.models.find((item: { id: string }) => item.id === identifier);
    Object.assign(model, { ready: true, runtime_ready: true, directory_exists: true,
      download: { status: "failed", message: "旧 OCR 下载错误", error: { message: "旧 OCR 下载错误", error_id: "old-ocr-download" } } });
    catalog.active_download = "whisper-base";
    const saved = { ...catalog.settings, ocr_option: identifier };
    const operations: string[] = [];
    let reads = 0;
    await page.route("**/api/v1/settings/recognition", async route => {
      if (route.request().method() === "PUT") {
        expect(route.request().postDataJSON()).toEqual(saved);
        operations.push("save");
        catalog.settings = saved;
      } else reads++;
      await route.fulfill({ json: catalog });
    });
    await page.route("**/api/v1/settings/models/**", async route => {
      expect(route.request().url()).toContain(`/models/${identifier}/reveal`);
      expect(route.request().method()).toBe("POST");
      expect(route.request().postData()).toBeNull();
      operations.push("reveal");
      await route.fulfill({ json: { opened: true } });
    });
    await page.clock.install();
    await page.goto("/#settings");
    const dialog = page.getByRole("dialog", { name: "设置", exact: true });
    await dialog.getByRole("tab", { name: "OCR 模型", exact: true }).click();
    const select = dialog.getByRole("combobox", { name: "OCR 模型", exact: true });
    await select.selectOption(identifier);
    const card = dialog.locator(".settings-card");
    const detail = card.locator(".local-model-detail");
    await expect(select.locator("option")).toHaveCount(1);
    await expect(detail.locator("dt")).toHaveText(["模型文件", "文件大小"]);
    await expect(detail.locator("p").first()).toHaveText("适用于中文、英文，适合清晰 PPT 和视频截图。");
    await expect(detail).toContainText("约 32 MB");
    await expect(detail.locator("a")).toHaveCount(0);
    await expect(card.getByRole("button")).toHaveText(["打开模型文件夹", "保存识别设置"]);
    await expect(card).not.toContainText(/下载|准备模型文件|旧 OCR 下载错误|运行引擎|保存位置/);
    await expect(card.getByRole("progressbar")).toHaveCount(0);
    await expect(card).toContainText("保存设置后，才会切换 OCR 模型。");
    const initialReads = reads;
    await page.clock.runFor(10000);
    expect(reads).toBe(initialReads);
    await expect(page.locator(".error-notification")).toHaveCount(0);
    const reveal = card.getByRole("button", { name: "打开模型文件夹", exact: true });
    expect(await reveal.evaluate(node => !node.closest(".local-model-detail"))).toBe(true);
    await reveal.focus();
    await page.keyboard.press("Enter");
    await expect(reveal).toBeEnabled();
    expect(operations).toEqual(["reveal"]);
    for (const width of [1280, 375]) {
      await page.setViewportSize({ width, height: 871 });
      for (const theme of ["light", "dark"]) {
        await page.evaluate(value => document.documentElement.setAttribute("data-theme", value), theme);
        await card.screenshot({ path: testInfo.outputPath(`ocr-${identifier}-${width}-${theme}.png`) });
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
      }
    }
    await card.getByRole("button", { name: "保存识别设置" }).click();
    await expect(card.getByRole("status").filter({ hasText: "识别设置已保存" })).toBeVisible();
    expect(operations).toEqual(["reveal", "save"]);
    await page.reload();
    await dialog.getByRole("tab", { name: "OCR 模型", exact: true }).click();
    await expect(select).toHaveValue(identifier);
    await expect(card.locator("p").filter({ hasText: "当前使用：" })).toContainText(model.name);
  });
}

test("OCR 打开与保存错误常驻，切换页面隔离迟到错误，成功后清除", async ({ page, request }) => {
  const catalog = await (await request.get("/api/v1/settings/recognition")).json();
  for (const model of catalog.models.filter((item: { kind: string }) => item.kind === "ocr")) {
    Object.assign(model, { ready: true, runtime_ready: true, directory_exists: true });
  }
  catalog.settings.ocr_option = "rapidocr";
  catalog.active_download = null;
  let failOpen = true;
  let failSave = true;
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  let delayed = false;
  let opened = 0;
  let saves = 0;
  await page.route("**/api/v1/settings/recognition", async route => {
    if (route.request().method() === "PUT") {
      saves++;
      if (failSave) {
        await route.fulfill({ status: 500, json: { error: { message: "模拟 OCR 保存失败", error_id: "ocr-save", log_file: "log/ocr-save.log" } } });
        return;
      }
      catalog.settings = route.request().postDataJSON();
    }
    await route.fulfill({ json: catalog });
  });
  await page.route("**/api/v1/settings/models/*/reveal", async route => {
    opened++;
    if (delayed) await gate;
    await route.fulfill(failOpen ? { status: 500, json: { error: { message: "模拟 OCR 文件夹打开失败", error_id: "ocr-open", log_file: "log/ocr-open.log" } } }
      : { json: { opened: true } });
  });
  await page.clock.install();
  await page.goto("/#settings");
  const dialog = page.getByRole("dialog", { name: "设置", exact: true });
  await dialog.getByRole("tab", { name: "OCR 模型", exact: true }).click();
  const card = dialog.locator(".settings-card");
  const reveal = card.getByRole("button", { name: "打开模型文件夹" });
  const save = card.getByRole("button", { name: "保存识别设置" });
  const error = card.getByRole("alert");
  await reveal.click();
  await expect(error).toContainText("log/ocr-open.log");
  await page.clock.runFor(10000);
  await expect(error).toBeVisible();
  expect(opened).toBe(1);
  await expect(page.locator(".error-notification")).toHaveCount(0);
  failOpen = false;
  await reveal.click();
  await expect(error).toHaveCount(0);
  failOpen = true;
  delayed = true;
  await reveal.click();
  await expect(reveal).toBeDisabled();
  await dialog.getByRole("tab", { name: "API设置", exact: true }).click();
  release();
  await dialog.getByRole("tab", { name: "OCR 模型", exact: true }).click();
  await expect(save).toBeEnabled();
  await expect(error).toHaveCount(0);
  await save.click();
  await expect(error).toContainText("log/ocr-save.log");
  await expect(error).not.toContainText("ocr-open");
  await page.clock.runFor(10000);
  await expect(error).toBeVisible();
  expect(saves).toBe(1);
  failSave = false;
  await save.click();
  await expect(error).toHaveCount(0);
  await expect(card.getByRole("status").filter({ hasText: "识别设置已保存" })).toBeVisible();
  expect(saves).toBe(2);
});

test("OCR 缺少文件或组件禁止保存，目录不存在时不能打开", async ({ page, request }) => {
  const catalog = await (await request.get("/api/v1/settings/recognition")).json();
  const rapidocr = catalog.models.find((item: { id: string }) => item.id === "rapidocr");
  Object.assign(rapidocr, { ready: true, runtime_ready: false, directory_exists: true });
  catalog.settings.ocr_option = "rapidocr";
  await page.route("**/api/v1/settings/recognition", route => {
    expect(route.request().method()).toBe("GET");
    return route.fulfill({ json: catalog });
  });
  await page.goto("/#settings");
  const dialog = page.getByRole("dialog", { name: "设置", exact: true });
  await dialog.getByRole("tab", { name: "OCR 模型", exact: true }).click();
  const card = dialog.locator(".settings-card");
  await expect(card.getByRole("button", { name: "保存识别设置" })).toBeDisabled();
  await expect(card.getByRole("status")).toContainText("识别组件不可用");
  await expect(card.getByRole("button", { name: "打开模型文件夹" })).toBeEnabled();
  Object.assign(rapidocr, { ready: false, runtime_ready: true, directory_exists: false });
  await page.reload();
  await dialog.getByRole("tab", { name: "OCR 模型", exact: true }).click();
  await expect(card.getByRole("button", { name: "保存识别设置" })).toBeDisabled();
  await expect(card.getByRole("button", { name: "打开模型文件夹" })).toBeDisabled();
  await expect(card.locator("dd").first()).toHaveText("尚未准备好");
});

test("语音识别仅保留三个模型，精简详情仍保留下载进度和保存检查", async ({ page, request }, testInfo) => {
  const catalog = await (await request.get("/api/v1/settings/recognition")).json();
  const paraformer = catalog.models.find((model: { id: string }) => model.id === "paraformer");
  const base = catalog.models.find((model: { id: string }) => model.id === "whisper-base");
  paraformer.ready = true;
  paraformer.runtime_ready = false;
  paraformer.download = { status: "succeeded", message: "模型文件已准备好，可以保存设置。", downloaded_bytes: 100, total_bytes: 100 };
  base.ready = false;
  base.runtime_ready = true;
  base.download = null;
  catalog.settings.asr_option = "paraformer";
  catalog.active_download = null;
  const saves: unknown[] = [];
  let downloads = 0;
  await page.route("**/api/v1/settings/recognition", async route => {
    if (route.request().method() === "PUT") {
      catalog.settings = route.request().postDataJSON();
      saves.push(catalog.settings);
    }
    await route.fulfill({ json: catalog });
  });
  await page.route("**/api/v1/settings/models/*/download", async route => {
    expect(route.request().method()).toBe("POST");
    expect(route.request().url()).toContain("/whisper-base/download");
    downloads++;
    catalog.active_download = "whisper-base";
    base.download = { status: "downloading", message: "正在下载模型文件…", downloaded_bytes: 524288, total_bytes: 1048576 };
    await route.fulfill({ json: base.download });
  });
  await page.clock.install();
  await page.goto("/#settings");
  const dialog = page.getByRole("dialog", { name: "设置", exact: true });
  await dialog.getByRole("tab", { name: "语音识别模型", exact: true }).click();
  const select = dialog.getByRole("combobox", { name: "语音识别模型", exact: true });
  const save = dialog.getByRole("button", { name: "保存识别设置" });
  const detail = dialog.locator(".local-model-detail");
  await expect(select.locator("option")).toHaveCount(3);
  expect(await select.locator("option").evaluateAll(options => options.map(option => (option as HTMLOptionElement).value)))
    .toEqual(["paraformer", "whisper-base", "whisper-small"]);
  await expect(detail.locator("p").first()).toHaveText("适用于中文（普通话）。中文优先，侧重速度和较低资源占用。");
  await expect(detail.locator("dt")).toHaveText(["模型文件", "文件大小"]);
  await expect(detail.locator("a")).toHaveCount(0);
  await expect(detail).not.toContainText("模型文件已准备好，可以保存设置。");
  await expect(detail).toContainText("识别组件不可用");
  await expect(save).toBeDisabled();

  paraformer.runtime_ready = true;
  await page.reload();
  await dialog.getByRole("tab", { name: "语音识别模型", exact: true }).click();
  await expect(save).toBeEnabled();
  await expect(detail).not.toContainText("识别组件不可用");
  for (const width of [1280, 375]) {
    await page.setViewportSize({ width, height: 871 });
    for (const theme of ["light", "dark"]) {
      await page.evaluate(value => document.documentElement.setAttribute("data-theme", value), theme);
      await dialog.screenshot({ path: testInfo.outputPath(`asr-${width}-${theme}.png`) });
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    }
  }
  await select.selectOption("whisper-small");
  await expect(detail.locator("p").first()).toHaveText("适用于中文、英文等多种语言。更重视准确率，耗时更长、资源占用更高。");
  await expect(detail).toContainText("约 486 MB");
  await expect(save).toBeDisabled();
  await detail.screenshot({ path: testInfo.outputPath("small-position-375.png") });
  await select.selectOption("whisper-base");
  await expect(detail.locator("p").first()).toHaveText("适用于中文、英文等多种语言。速度与准确率折中，资源占用较低。");
  await expect(save).toBeDisabled();
  await dialog.getByRole("button", { name: "下载模型到 models" }).click();
  await expect(detail.getByRole("progressbar", { name: "模型下载进度" })).toHaveAttribute("value", "524288");
  await expect(save).toBeDisabled();
  expect(catalog.settings.asr_option).toBe("paraformer");
  base.ready = true;
  base.download = { ...paraformer.download };
  catalog.active_download = null;
  await page.clock.runFor(2100);
  await expect(detail.getByRole("progressbar")).toHaveCount(0);
  await expect(detail).not.toContainText("模型文件已准备好，可以保存设置。");
  await expect(save).toBeEnabled();
  await save.click();
  await expect(dialog.getByRole("status").filter({ hasText: "识别设置已保存" })).toBeVisible();
  expect(saves).toEqual([{ ...catalog.settings, asr_option: "whisper-base" }]);
  expect(downloads).toBe(1);
});

test("语音模型下载删除并排，打开文件夹位于详情下方，确认删除和失败重试", async ({ page, request }, testInfo) => {
  const catalog = await (await request.get("/api/v1/settings/recognition")).json();
  const model = catalog.models.find((item: { id: string }) => item.id === "whisper-base");
  Object.assign(model, { ready: true, runtime_ready: true, directory_exists: true });
  catalog.settings.asr_option = "whisper-base";
  catalog.active_download = null;
  const operations: string[] = [];
  let failed = true;
  await page.route("**/api/v1/settings/recognition", route => route.fulfill({ json: catalog }));
  await page.route("**/api/v1/settings/models/*", async route => {
    expect(route.request().method()).toBe("DELETE");
    expect(route.request().postData()).toBeNull();
    expect(route.request().url()).toContain("/whisper-base");
    operations.push("delete");
    if (failed) {
      await route.fulfill({ status: 409, json: { error: { code: "model_in_use", message: "模拟任务正在使用模型", error_id: "model-in-use", log_file: "log/model-in-use.log" } } });
    } else {
      Object.assign(model, { ready: false, directory_exists: false, download: null });
      await route.fulfill({ json: { deleted: true } });
    }
  });
  await page.route("**/api/v1/settings/models/*/reveal", async route => {
    expect(route.request().method()).toBe("POST");
    expect(route.request().postData()).toBeNull();
    expect(route.request().url()).toContain("/whisper-base/reveal");
    operations.push("reveal");
    await route.fulfill({ json: { opened: true } });
  });
  await page.goto("/#settings");
  const settings = page.getByRole("dialog", { name: "设置", exact: true });
  await settings.getByRole("tab", { name: "语音识别模型", exact: true }).click();
  const download = settings.getByRole("button", { name: "重新下载 / 检查文件" });
  const reveal = settings.getByRole("button", { name: "打开模型文件夹" });
  const remove = settings.getByRole("button", { name: "删除模型文件" });
  for (const width of [1280, 850, 375]) {
    await page.setViewportSize({ width, height: 871 });
    for (const theme of ["light", "dark"]) {
      await page.evaluate(value => document.documentElement.setAttribute("data-theme", value), theme);
      const boxes = await Promise.all([download, remove, reveal].map(button => button.boundingBox()));
      const [first, second, third] = boxes.map(box => box!);
      expect(first.x + first.width).toBeLessThanOrEqual(second.x);
      expect(Math.abs((first.y + first.height / 2) - (second.y + second.height / 2))).toBeLessThanOrEqual(1);
      const detail = (await settings.locator(".local-model-detail").boundingBox())!;
      expect(third.y).toBeGreaterThanOrEqual(detail.y + detail.height);
      expect(await settings.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
      await settings.screenshot({ path: testInfo.outputPath(`model-actions-${width}-${theme}.png`) });
    }
  }
  await reveal.click();
  expect(operations).toEqual(["reveal"]);
  await remove.click();
  const confirmation = page.getByRole("dialog", { name: "删除模型文件？", exact: true });
  await expect(confirmation).toContainText("faster-whisper · base");
  await expect(confirmation).toContainText("这是当前使用的语音识别模型");
  await confirmation.getByRole("button", { name: "取消", exact: true }).click();
  await expect(confirmation).toHaveCount(0);
  expect(operations).toEqual(["reveal"]);
  await remove.focus();
  await page.keyboard.press("Enter");
  await confirmation.getByRole("button", { name: "确认删除", exact: true }).click();
  await expect(page.getByRole("alert").filter({ hasText: "模拟任务正在使用模型" })).toBeVisible();
  await expect(confirmation.getByRole("button", { name: "确认删除", exact: true })).toBeEnabled();
  await expect(confirmation).toBeVisible();
  failed = false;
  await confirmation.getByRole("button", { name: "确认删除", exact: true }).click();
  await expect(confirmation).toHaveCount(0);
  await expect(reveal).toBeDisabled();
  await expect(remove).toBeDisabled();
  await expect(settings.getByRole("button", { name: "下载模型到 models" })).toBeEnabled();
  await expect(settings.getByRole("button", { name: "保存识别设置" })).toBeDisabled();
  expect(operations).toEqual(["reveal", "delete", "delete"]);
  expect(catalog.settings.asr_option).toBe("whisper-base");
  await settings.getByRole("tab", { name: "OCR 模型", exact: true }).click();
  await expect(settings.getByRole("button", { name: "删除模型文件" })).toHaveCount(0);
});

test("模型下载可暂停继续取消，刷新恢复暂停状态，关闭设置不取消下载", async ({ page, request }, testInfo) => {
  const catalog = await (await request.get("/api/v1/settings/recognition")).json();
  const model = catalog.models.find((item: { id: string }) => item.id === "whisper-base");
  Object.assign(model, { ready: false, runtime_ready: true, directory_exists: true,
    download: { status: "downloading", message: "正在从镜像下载模型…", downloaded_bytes: 524288, total_bytes: 1048576 } });
  catalog.settings.asr_option = "whisper-base";
  catalog.active_download = "whisper-base";
  const controls: string[] = [];
  await page.route("**/api/v1/settings/recognition", route => route.fulfill({ json: catalog }));
  await page.route("**/api/v1/settings/models/whisper-base/download/*", async route => {
    expect(route.request().method()).toBe("POST");
    const action = route.request().url().split("/").at(-1)!;
    controls.push(action);
    model.download.status = action === "pause" ? "pausing" : action === "resume" ? "downloading" : "cancelling";
    model.download.message = action === "pause" ? "正在暂停下载…" : action === "resume" ? "正在继续下载…" : "正在取消下载…";
    if (action === "resume") catalog.active_download = "whisper-base";
    await route.fulfill({ status: 202, json: { id: "whisper-base", status: model.download.status } });
  });
  await page.clock.install();
  await page.goto("/#settings");
  const settings = page.getByRole("dialog", { name: "设置", exact: true });
  await settings.getByRole("tab", { name: "语音识别模型", exact: true }).click();
  await settings.getByRole("button", { name: "暂停下载", exact: true }).click();
  await expect(settings.getByRole("button", { name: "正在暂停…" })).toBeDisabled();
  Object.assign(model.download, { status: "paused", message: "下载已暂停，可以继续下载。" });
  catalog.active_download = null;
  await page.clock.runFor(2100);
  await expect(settings.getByRole("button", { name: "继续下载", exact: true })).toBeEnabled();
  await expect(settings.getByRole("progressbar", { name: "模型下载进度" })).toHaveAttribute("value", "524288");
  await page.reload();
  await settings.getByRole("tab", { name: "语音识别模型", exact: true }).click();
  await expect(settings.getByRole("button", { name: "继续下载", exact: true })).toBeEnabled();
  for (const width of [1280, 375]) {
    await page.setViewportSize({ width, height: 871 });
    for (const theme of ["light", "dark"]) {
      await page.evaluate(value => document.documentElement.setAttribute("data-theme", value), theme);
      await settings.screenshot({ path: testInfo.outputPath(`download-paused-${width}-${theme}.png`) });
      expect(await settings.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
    }
  }
  await settings.getByRole("button", { name: "继续下载", exact: true }).click();
  await expect(settings.getByRole("button", { name: "暂停下载", exact: true })).toBeEnabled();
  await expect(settings.getByRole("progressbar", { name: "模型下载进度" })).toHaveAttribute("value", "524288");
  await settings.getByRole("button", { name: "关闭设置", exact: true }).click();
  await expect(settings).toHaveCount(0);
  expect(controls).toEqual(["pause", "resume"]);
  await page.goto("/#settings");
  await settings.getByRole("tab", { name: "语音识别模型", exact: true }).click();
  await settings.getByRole("button", { name: "取消下载", exact: true }).click();
  await expect(settings.getByRole("button", { name: "正在取消…" })).toBeDisabled();
  Object.assign(model.download, { status: "cancelled", message: "下载已取消。", downloaded_bytes: 0, total_bytes: null });
  catalog.active_download = null;
  await page.clock.runFor(2100);
  await expect(settings.getByRole("button", { name: "下载模型到 models" })).toBeEnabled();
  await expect(settings.getByRole("button", { name: "继续下载", exact: true })).toHaveCount(0);
  await expect(settings.getByRole("progressbar")).toHaveCount(0);
  expect(controls).toEqual(["pause", "resume", "cancel"]);
});

for (const action of ["pause", "cancel"] as const) {
  test(`模型下载控制失败保留原进度并可手动重试：${action}`, async ({ page, request }) => {
    const catalog = await (await request.get("/api/v1/settings/recognition")).json();
    const model = catalog.models.find((item: { id: string }) => item.id === "whisper-base");
    Object.assign(model, { ready: false, runtime_ready: true, directory_exists: true,
      download: { status: "downloading", message: "正在下载…", downloaded_bytes: 524288, total_bytes: 1048576 } });
    catalog.settings.asr_option = "whisper-base";
    catalog.active_download = "whisper-base";
    let attempts = 0;
    await page.route("**/api/v1/settings/recognition", route => route.fulfill({ json: catalog }));
    await page.route(`**/api/v1/settings/models/whisper-base/download/${action}`, async route => {
      attempts++;
      if (attempts === 1) {
        await route.fulfill({ status: 503, json: { error: { code: "download_interrupted", message: "模拟控制请求失败", error_id: `control-${action}`, log_file: `log/control-${action}.log` } } });
      } else {
        catalog.active_download = null;
        Object.assign(model.download, { status: action === "pause" ? "paused" : "cancelled", message: action === "pause" ? "下载已暂停" : "下载已取消" });
        await route.fulfill({ status: 202, json: { id: "whisper-base", status: model.download.status } });
      }
    });
    await page.clock.install();
    await page.goto("/#settings");
    const settings = page.getByRole("dialog", { name: "设置", exact: true });
    await settings.getByRole("tab", { name: "语音识别模型", exact: true }).click();
    const button = settings.getByRole("button", { name: action === "pause" ? "暂停下载" : "取消下载", exact: true });
    await button.click();
    await expect(page.locator(".error-notification")).toContainText("模拟控制请求失败");
    await expect(settings.locator(".model-download-state.task-error")).toContainText("模拟控制请求失败");
    await page.mouse.move(0, 800);
    await page.clock.runFor(6000);
    await expect(page.locator(".error-notification")).toHaveCount(0);
    await expect(settings.locator(".model-download-state.task-error")).toBeVisible();
    await expect(button).toBeEnabled();
    await expect(settings.getByRole("progressbar")).toHaveAttribute("value", "524288");
    expect(attempts).toBe(1);
    await button.click();
    await expect(settings.getByRole("button", { name: action === "pause" ? "继续下载" : "下载模型到 models", exact: true })).toBeEnabled();
    await expect(settings.locator(".model-download-state.task-error")).toHaveCount(0);
    expect(attempts).toBe(2);
  });
}

test("模型卡片只保留当前下载错误，重试立即清空且刷新后仍可查看", async ({ page, request }, testInfo) => {
  const catalog = await (await request.get("/api/v1/settings/recognition")).json();
  const model = catalog.models.find((item: { id: string }) => item.id === "whisper-base");
  Object.assign(model, { ready: false, runtime_ready: true, directory_exists: true,
    download: { status: "failed", message: "第一次下载失败", downloaded_bytes: 0, total_bytes: null,
      error: { message: "第一次下载失败", error_id: "previous-download", log_file: "log/previous-download.log" } } });
  catalog.settings.asr_option = "whisper-base";
  catalog.active_download = null;
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  let attempts = 0;
  await page.route("**/api/v1/settings/recognition", route => route.fulfill({ json: catalog }));
  await page.route("**/api/v1/settings/models/whisper-base/download", async route => {
    expect(route.request().method()).toBe("POST");
    attempts++;
    await gate;
    model.download = { ...model.download, message: "当前下载失败", error: { message: "当前下载失败", error_id: "current-download", log_file: "log/current-download.log" } };
    await route.fulfill({ status: 202, json: { id: "whisper-base", status: "downloading" } });
  });
  await page.clock.install();
  await page.goto("/#settings");
  const settings = page.getByRole("dialog", { name: "设置", exact: true });
  await settings.getByRole("tab", { name: "语音识别模型", exact: true }).click();
  const error = settings.locator(".model-download-state.task-error");
  await expect(error).toContainText("第一次下载失败");
  await settings.getByRole("button", { name: "下载模型到 models" }).click();
  await expect(error).toHaveCount(0);
  release();
  await expect(error).toHaveCount(1);
  await expect(error).toContainText("current-download");
  await expect(error).not.toContainText("previous-download");
  await page.mouse.move(0, 800);
  await page.clock.runFor(10000);
  await expect(error).toBeVisible();
  await expect(page.locator(".error-notification")).toHaveCount(0);
  await settings.getByRole("combobox", { name: "语音识别模型", exact: true }).selectOption("paraformer");
  await expect(error).toHaveCount(0);
  await settings.getByRole("combobox", { name: "语音识别模型", exact: true }).selectOption("whisper-base");
  await expect(error).toContainText("current-download");
  await page.reload();
  await settings.getByRole("tab", { name: "语音识别模型", exact: true }).click();
  await expect(error).toContainText("log/current-download.log");
  await page.clock.runFor(10000);
  await expect(error).toBeVisible();
  for (const width of [1280, 375]) {
    await page.setViewportSize({ width, height: 871 });
    for (const theme of ["light", "dark"]) {
      await page.evaluate(value => document.documentElement.setAttribute("data-theme", value), theme);
      await settings.screenshot({ path: testInfo.outputPath(`persistent-download-error-${width}-${theme}.png`) });
      expect(await settings.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
    }
  }
  expect(attempts).toBe(1);
});

test("删除模型期间禁止重复提交，关闭设置后忽略迟到响应", async ({ page, request }) => {
  const catalog = await (await request.get("/api/v1/settings/recognition")).json();
  const model = catalog.models.find((item: { id: string }) => item.id === "whisper-base");
  Object.assign(model, { ready: true, runtime_ready: true, directory_exists: true });
  catalog.settings.asr_option = "whisper-base";
  catalog.active_download = null;
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  let delivered!: () => void;
  const finished = new Promise<void>(resolve => { delivered = resolve; });
  let deletions = 0;
  await page.route("**/api/v1/settings/recognition", route => route.fulfill({ json: catalog }));
  await page.route("**/api/v1/settings/models/whisper-base", async route => {
    expect(route.request().method()).toBe("DELETE");
    deletions++;
    await gate;
    await route.fulfill({ json: { deleted: true } });
    delivered();
  });
  await page.goto("/#settings");
  const settings = page.getByRole("dialog", { name: "设置", exact: true });
  await settings.getByRole("tab", { name: "语音识别模型", exact: true }).click();
  await settings.getByRole("button", { name: "删除模型文件" }).click();
  const confirmation = page.getByRole("dialog", { name: "删除模型文件？", exact: true });
  await confirmation.getByRole("button", { name: "确认删除", exact: true }).click();
  await expect(confirmation.getByRole("button", { name: "正在删除…" })).toBeDisabled();
  await page.keyboard.press("Escape");
  await expect(confirmation).toBeVisible();
  await page.evaluate(() => { window.location.hash = "#summary"; });
  await expect(settings).toHaveCount(0);
  release();
  await finished;
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await expect(page.getByRole("alert").filter({ hasText: "模型文件已删除" })).toHaveCount(0);
  expect(deletions).toBe(1);
});

test("页面异常生成本机日志并显示位置，服务离线时不声称已保存", async ({
  page,
}) => {
  await page.goto("/#summary");
  await page.waitForLoadState("networkidle");
  const logged = page.waitForResponse(
    (response) =>
      response.url().endsWith("/diagnostics/client-errors") &&
      response.status() === 201,
  );
  await page.evaluate(() =>
    window.dispatchEvent(
      new ErrorEvent("error", { error: new Error("模拟前端故障，验证日志") }),
    ),
  );
  const response = await logged;
  const reference = await response.json();
  await expect(
    page.getByRole("alert").filter({ hasText: reference.error_id }),
  ).toContainText(reference.log_file);
  expect(readFileSync(join(dirname(logPath), basename(reference.log_file)), "utf8")).toContain(
    reference.error_id,
  );
  await page.route("**/api/v1/**", (route) => route.abort());
  await page.evaluate(() =>
    window.dispatchEvent(
      new ErrorEvent("error", { error: new Error("模拟离线故障") }),
    ),
  );
  await expect(
    page.getByRole("alert").filter({ hasText: "详细日志暂未保存" }),
  ).toBeVisible();
});

test("上传、等待总结、核对截图、时间跳转与刷新持久化", async ({ page }) => {
  await page.request.put("/api/v1/settings/model", { data: { token: "fake-upload-flow" } });
  const file = { name: "upload-flow.mp4", mimeType: "video/mp4", buffer: Buffer.concat([readFileSync(samplePath), Buffer.from(`upload-flow-${Date.now()}`)]) };
  await page.goto("/#summary");
  await page.waitForLoadState("networkidle");
  const created = page.waitForResponse(
    (response) =>
      response.url().includes("/analyses") &&
      response.request().method() === "POST",
  );
  await page
    .getByLabel("选择本地文件")
    .setInputFiles(file);
  await page.getByRole("button", { name: "开始处理", exact: true }).click();
  await expect(
    page.getByRole("dialog", { name: "选择处理模型" }),
  ).toBeVisible();
  await page.getByRole("dialog", { name: "选择处理模型" }).getByRole("button", { name: "开始处理", exact: true }).click();
  const response = await created;
  const videoId = new URL(response.url()).pathname.split("/").at(-2)!;
  const { task_id } = await response.json();
  await expect(
    page.getByText("这是自动化测试返回的总结：视频介绍 TCP 慢启动。"),
  ).toBeVisible({ timeout: 45000 });
  await expect(page.getByRole("region", { name: "文件上传" })).toHaveCount(0);
  await expect(
    page.locator(".detail-back"),
  ).toBeVisible();
  await expect(page.locator(".overview p")).toHaveCount(2);
  await expect(page.locator(".overview h3")).toHaveText("TCP 慢启动与拥塞窗口增长");
  await expect(page.getByRole("list", { name: "关键标签" }).getByRole("listitem")).toHaveText(["#TCP", "#慢启动", "#拥塞窗口"]);
  await expect(page.getByRole("heading", { name: "先看整体", exact: true })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "原始转录" })).toBeVisible();
  await expect(page.locator(".summary-panel .warning")).toHaveCount(0);
  await expect(page.locator(".summary-footnote")).toHaveCount(0);
  const transcript = await page.request.get(
    (await page
    .locator(".transcript-actions").getByRole("link", { name: "下载 TXT" })
      .getAttribute("href")) as string,
  );
  expect(transcript.status()).toBe(200);
  expect(await transcript.text()).toContain("慢启动让拥塞窗口逐步增长。");
  await expect(page.getByRole("progressbar")).toHaveAttribute(
    "aria-valuenow",
    "100",
  );
  await expect(page.locator(".workflow-steps > li")).toHaveCount(6);
  await expect(page.locator(".workflow-steps > li.done")).toHaveCount(6);
  await page.getByRole("button", { name: "查看本章依据", exact: true }).click();
  const citation = page.locator("#block-evidence-C1");
  await expect(citation.locator("img").first()).toBeVisible();
  const evidence = await (await page.request.get(`/api/v1/videos/${videoId}/evidence?task_id=${task_id}`)).json();
  const frame = evidence.find((item: { kind: string; start_seconds: number }) => item.kind === "frame" && item.start_seconds > 0);
  expect(frame).toBeTruthy();
  const timestamp = `${Math.floor(frame.start_seconds / 60).toString().padStart(2, "0")}:${Math.floor(frame.start_seconds % 60).toString().padStart(2, "0")}`;
  await citation.getByRole("button", { name: `${timestamp} ↗`, exact: true }).click();
  await expect(page.getByText(`已定位至 ${timestamp}`)).toBeVisible();
  await expect
    .poll(() =>
      page.locator("video").evaluate((v: HTMLVideoElement) => v.currentTime),
    )
    .toBeCloseTo(frame.start_seconds, 0);
  await page.reload();
  await expect(
    page.getByText("这是自动化测试返回的总结：视频介绍 TCP 慢启动。"),
  ).toBeVisible();
  await page.locator(".detail-back").click();
  await page.getByLabel("选择本地文件").setInputFiles(file);
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.locator(".overview p")).toHaveCount(2);
  await expect(page.getByRole("region", { name: "文件上传" })).toHaveCount(0);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({
    path: test.info().outputPath("browser-desktop.png"),
    fullPage: true,
  });
});

test("损坏的视频显示明确错误，不生成总结", async ({ page }) => {
  await page.goto("/#summary");
  await page.waitForLoadState("networkidle");
  const rejected = page.waitForResponse(response => response.url().endsWith("/videos") && response.request().method() === "POST");
  await page.getByLabel("选择本地文件").setInputFiles({
    name: "broken.mp4",
    mimeType: "video/mp4",
    buffer: Buffer.from("not a real video"),
  });
  const response = await rejected;
  expect(response.status()).toBe(400);
  const { error } = await response.json();
  await expect(page.getByRole("alert")).toContainText(error.message);
  await expect(page.getByRole("alert")).toContainText(error.error_id);
  await expect(page).toHaveURL(/#summary$/);
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

test("桌面首页两个入口与笔记中心", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator(".home-options").getByRole("button", { name: "笔记中心" })).toBeVisible();
  await expect(page.getByRole("button", { name: "开始总结？" })).toBeVisible();
  await page.screenshot({
    path: test.info().outputPath("home-desktop.png"),
    fullPage: true,
  });
  await page.locator(".home-options").getByRole("button", { name: "笔记中心" }).click();
  await expect(
    page.getByRole("heading", { name: "笔记中心", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("searchbox", { name: "搜索笔记" }),
  ).toBeVisible();
  await page.getByRole("link", { name: "新的开始", exact: false }).click();
  await page.getByRole("button", { name: "开始总结？" }).click();
  await expect(page.getByLabel("选择本地文件")).toBeAttached();
});

test("笔记补充要求可留空、编辑保存并清空", async ({ page, request }) => {
  const previous = await (await request.get("/api/v1/settings/note-style")).json();
  await request.put("/api/v1/settings/note-style", { data: { note_style: "detailed", summary_prompt: "" } });
  try {
  await page.goto("/#settings");
  const original = await (await page.request.get("/api/v1/settings/model")).json();
  await expect(page.getByLabel("笔记补充要求（选填）")).toHaveCount(0);
  await page.getByLabel("模型 ID", { exact: true }).fill("test/unsaved-model");
  await page.getByLabel("API 密钥").fill("fake-unsaved-token");
  await page.getByRole("tab", { name: "笔记风格", exact: true }).click();
  const prompt = page.getByLabel("笔记补充要求（选填）");
  await expect(prompt).toHaveValue("");
  await expect(page.getByRole("button", { name: "测试连接", exact: true })).toHaveCount(0);
  await prompt.fill("请重点解释视频的概念、适用条件和例子。");
  await page.getByRole("button", { name: "保存笔记风格" }).click();
  await expect(page.locator(".error-notification.success")).toContainText("笔记风格保存成功。");
  await expect(prompt).toHaveValue("请重点解释视频的概念、适用条件和例子。");
  expect(await (await page.request.get("/api/v1/settings/model")).json()).toEqual({
    ...original, summary_prompt: "请重点解释视频的概念、适用条件和例子。",
  });
  await page.getByRole("button", { name: "关闭成功通知" }).click();
  await page.getByRole("button", { name: "关闭设置", exact: true }).click();
  await page.getByRole("link", { name: "⚙ 设置", exact: true }).click();
  await page.getByRole("tab", { name: "笔记风格", exact: true }).click();
  await expect(prompt).toHaveValue("请重点解释视频的概念、适用条件和例子。");
  await page.getByRole("button", { name: "清空补充要求" }).click();
  await expect(prompt).toHaveValue("");
  await page.getByRole("button", { name: "保存笔记风格" }).click();
  await expect(page.locator(".error-notification.success")).toContainText("笔记风格保存成功。");
  await page.getByRole("button", { name: "关闭成功通知" }).click();
  await page.getByRole("button", { name: "关闭设置", exact: true }).click();
  await page.reload();
  await page.getByRole("link", { name: "⚙ 设置", exact: true }).click();
  await page.getByRole("tab", { name: "笔记风格", exact: true }).click();
  await expect(prompt).toHaveValue("");
  } finally { await request.put("/api/v1/settings/note-style", { data: previous }); }
});

test("语言模型并发设置保存后保留且不覆盖笔记风格", async ({ page }) => {
  await page.goto("/#settings");
  await page.getByText("高级设置", { exact: true }).click();
  const concurrent = page.getByLabel("并发请求数", { exact: true });
  await expect(concurrent).toHaveValue("3");
  await expect(concurrent.locator("option")).toHaveCount(4);

  const interval = page.getByLabel("请求发送间隔（秒）", { exact: true });
  await expect(interval).toHaveValue("3");
  await expect(interval).toHaveAttribute("min", "1");
  await expect(interval).toHaveAttribute("max", "60");

  // 模型页打开后独立保存风格，后续提交模型表单不能用旧快照覆盖风格。
  await page.request.put("/api/v1/settings/note-style", { data: { summary_prompt: "独立保存的笔记要求" } });
  await concurrent.selectOption("2");
  await interval.fill("0");
  await page.getByRole("button", { name: "保存配置", exact: true }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  expect(await interval.evaluate(element => (element as HTMLInputElement).validity.rangeUnderflow)).toBe(true);
  await interval.fill("7");
  const saved = page.waitForRequest(request => request.method() === "PUT" && new URL(request.url()).pathname.startsWith("/api/v1/settings/model/profiles/"));
  await page.getByRole("button", { name: "保存配置", exact: true }).click();
  const body = (await saved).postDataJSON();
  expect(body.section_concurrency).toBe(2);
  expect(body.request_interval_seconds).toBe(7);
  expect(body).not.toHaveProperty("summary_prompt");
  await expect(page.locator(".notice[role='status']")).toContainText("配置已保存");
  expect(await (await page.request.get("/api/v1/settings/note-style")).json()).toEqual({ summary_prompt: "独立保存的笔记要求", note_style: "detailed" });
  await page.reload();
  await page.getByText("高级设置", { exact: true }).click();
  await expect(concurrent).toHaveValue("2");
  await expect(interval).toHaveValue("7");
  await concurrent.selectOption("3");
  await interval.fill("3");
  await page.getByRole("button", { name: "保存配置", exact: true }).click();
  await expect(page.locator(".notice[role='status']")).toContainText("配置已保存");
  await page.request.put("/api/v1/settings/note-style", { data: { summary_prompt: "" } });
});

test("转录分页、依据排序与展开不改变转录分类", async ({ page }) => {
  const video = await (
    await page.request.post("/api/v1/videos", {
      multipart: {
        file: {
          name: "transcript-tabs.mp4",
          mimeType: "video/mp4",
          buffer: readFileSync(samplePath),
        },
      },
    })
  ).json();
  const created = await (
    await page.request.post(`/api/v1/videos/${video.id}/analyses`)
  ).json();
  await expect
    .poll(
      async () =>
        (
          await (
            await page.request.get(`/api/v1/tasks/${created.task_id}`)
          ).json()
        ).status,
      { timeout: 45000 },
    )
    .toBe("succeeded");
  video.latest_task = await (
    await page.request.get(`/api/v1/tasks/${created.task_id}`)
  ).json();
  const raw = await (
    await page.request.get(
      `/api/v1/videos/${video.id}/evidence?task_id=${video.latest_task.id}`,
    )
  ).json();
  const frames = raw.filter((item: { kind: string }) => item.kind === "frame");
  const speech = raw.find((item: { kind: string }) => item.kind === "speech");
  // 故意倒序，并增加转录以验证分页；只在当前测试页面替换返回值。
  const records = [
    ...raw,
    ...Array.from({ length: 35 }, (_, index) => ({
      ...speech,
      id: `test-speech-${index}`,
      start_seconds: index + 2,
      end_seconds: index + 2.5,
      text: `测试转录 ${index + 1}`,
    })),
  ].reverse();
  const original = await (await page.request.get(`/api/v1/analyses/${video.latest_task.id}`)).json();
  const heading = original.chapters[0];
  const result = {
    ...original,
    evidence_ids: records.map(item => item.id),
    chapters: [
      { ...heading, markdown: "三个画面引用", evidence_ids: [frames.at(-1).id, frames[0].id, frames[2].id] },
      { ...heading, chapter_id: "C2", markdown: "一条语音引用", evidence_ids: [speech.id] },
    ],
  };
  await page.route("**/api/v1/videos/*/evidence*", (route) =>
    route.fulfill({ json: records }),
  );
  await page.route("**/api/v1/analyses/*", (route) =>
    route.fulfill({ json: result }),
  );
  await page.goto(`/#summary/${video.id}`);
  await expect(page.locator(".summary-section > .cite-button,.note-content .cite-button")).toHaveCount(0);
  await expect(page.locator(".note-subsection > .cite-button")).toHaveText([
    "查看本章依据", "查看本章依据",
  ]);
  await expect(page.getByRole("tab", { name: "语音转录" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(page.locator(".evidence-item")).toHaveCount(30);
  await expect(page.locator(".evidence-type")).toHaveText(
    Array(30).fill("语音识别"),
  );
  const more = page.getByRole("button", { name: "显示更多转录" });
  const buttonBox = (await more.boundingBox())!;
  const panelBox = (await page.locator(".evidence-panel").boundingBox())!;
  expect(
    Math.abs(
      buttonBox.x + buttonBox.width / 2 - panelBox.x - panelBox.width / 2,
    ),
  ).toBeLessThan(1);
  await more.click();
  await expect(page.locator(".evidence-item")).toHaveCount(36);
  await page.locator(".evidence-list").evaluate((element) => {
    element.scrollTop = element.scrollHeight;
  });
  await page.getByRole("button", { name: "查看本章依据", exact: true }).nth(0).click();
  await expect(page.getByRole("tab", { name: "语音转录" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(page.locator(".evidence-item")).toHaveCount(36);
  const citation = page.locator("#block-evidence-C1");
  await expect(citation.locator("tbody tr")).toHaveCount(3);
  expect(await citation.locator("img").evaluateAll((images) => images.map((image) => image.getAttribute("src")))).toEqual([
    `/api/v1/evidence/${frames[0].id}/image`,
    `/api/v1/evidence/${frames[2].id}/image`,
    `/api/v1/evidence/${frames.at(-1).id}/image`,
  ]);
  await page.getByRole("button", { name: "查看本章依据", exact: true }).last().click();
  await expect(page.getByRole("tab", { name: "语音转录" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(page.locator("#block-evidence-C2 tbody tr")).toHaveCount(1);
  await expect(page.locator(".evidence-item")).toHaveCount(36);
  await page.getByRole("tab", { name: "图像识别" }).click();
  await expect(page.locator(".evidence-item")).toHaveCount(Math.min(30, frames.length));
  if (frames.length > 30) await page.getByRole("button", { name: "显示更多画面", exact: true }).click();
  await expect(page.locator(".evidence-item")).toHaveCount(frames.length);
});

test("上传后手动确认模型，取消不分析，确认后只覆盖本次任务", async ({ page }) => {
  await page.request.put("/api/v1/settings/model", { data: { token: "fake-setup-test" } });
  await page.goto("/#summary");
  await page.waitForLoadState("networkidle");
  const before = await (await page.request.get("/api/v1/settings/recognition")).json();
  const writes: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "POST" && (request.url().endsWith("/videos") || request.url().endsWith("/analyses"))) writes.push(request.url());
  });
  await page.getByLabel("选择本地文件").setInputFiles(samplePath);
  await expect(page).toHaveURL(/#summary\/[^/]+$/);
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(writes).toHaveLength(1);
  await page.getByRole("button", { name: "重新生成笔记", exact: true }).or(page.getByRole("button", { name: "开始处理", exact: true })).click();
  const dialog = page.getByRole("dialog", { name: "选择处理模型" });
  await expect(dialog.getByRole("button", { name: "开始处理", exact: true })).toBeEnabled();
  expect(writes).toHaveLength(1);
  await dialog.getByRole("button", { name: "取消", exact: true }).click();
  await expect(dialog).not.toBeVisible();
  expect(writes).toHaveLength(1);
  await page.getByRole("button", { name: "重新生成笔记", exact: true }).or(page.getByRole("button", { name: "开始处理", exact: true })).click();
  await dialog.getByLabel("语音识别模型", { exact: true }).selectOption("whisper-base");
  await expect(dialog.getByRole("button", { name: "开始处理", exact: true })).toBeDisabled();
  await dialog.getByLabel("语音识别模型", { exact: true }).selectOption("paraformer");
  await dialog.getByLabel("OCR 模型", { exact: true }).selectOption("rapidocr");
  const savedProfiles = await page.request.post("/api/v1/settings/model/profiles", { data: {
    name: "本次 API", base_url: "https://chosen.invalid/v1", token: "fake-per-task", model: "test/per-task-model",
  } });
  const profileId = (await savedProfiles.json()).profiles.at(-1).id;
  await dialog.getByRole("button", { name: "取消", exact: true }).click();
  await page.getByRole("button", { name: "重新生成笔记", exact: true }).or(page.getByRole("button", { name: "开始处理", exact: true })).click();
  await expect(dialog.getByLabel("视频语音语言", { exact: true })).toHaveCount(0);
  await expect(dialog.getByLabel("总结大模型", { exact: true })).toHaveCount(0);
  await expect(dialog.getByRole("button", { name: "读取当前 API 可用模型" })).toHaveCount(0);
  await dialog.getByLabel("OCR 模型", { exact: true }).selectOption("rapidocr");
  await dialog.getByRole("combobox", { name: "API 配置", exact: true }).selectOption(profileId);
  await dialog.getByRole("combobox", { name: /^笔记风格/ }).selectOption("detailed");
  await page.screenshot({ path: test.info().outputPath("analysis-setup-desktop.png"), fullPage: true });
  await page.setViewportSize({ width: 598, height: 871 });
  await expect.poll(() => dialog.evaluate(element => element.scrollWidth - element.clientWidth)).toBeLessThanOrEqual(1);
  await dialog.screenshot({ path: test.info().outputPath("analysis-setup-598.png") });
  expect(writes).toHaveLength(1);
  const submitted = page.waitForResponse((response) => response.url().endsWith("/analyses") && response.request().method() === "POST");
  await dialog.getByRole("button", { name: "开始处理", exact: true }).click();
  const response = await submitted;
  expect(response.status()).toBe(202);
  expect(response.request().postDataJSON()).toEqual({ asr_option: "paraformer", ocr_option: "rapidocr", llm_profile_id: profileId, note_style: "detailed" });
  await expect(dialog).not.toBeVisible();
  await expect(page.locator(".overview p")).toHaveCount(2, { timeout: 15000 });
  const taskId = (await response.json()).task_id;
  // 相同文件可能复用已有视频，旧版本正文出现不代表本次任务完成。
  await expect.poll(async () => (await (await page.request.get(`/api/v1/tasks/${taskId}`)).json()).status,
    { timeout: 15000 }).toBe("succeeded");
  const task = await (await page.request.get(`/api/v1/tasks/${taskId}`)).json();
  expect(task.config).toHaveProperty("extract_version");
  expect(Object.keys(task.config).every(key => ["extract_version", "outline_preview", "transcript_source"].includes(key))).toBe(true);
  const notes = await (await page.request.get(`/api/v1/analyses/${taskId}`)).json();
  expect(notes.model).toBe("test/per-task-model");
  expect(notes.recognition.ocr_engine).toBe("rapidocr");
  const after = await (await page.request.get("/api/v1/settings/recognition")).json();
  expect(after.settings).toEqual(before.settings);
  expect((await (await page.request.get("/api/v1/settings/model/profiles")).json()).active_id).not.toBe(profileId);
  await page.request.delete(`/api/v1/settings/model/profiles/${profileId}`);
  await expect(page.getByText("有据可查", { exact: true })).toHaveCount(0);
});

test("侧栏后台任务等待和运行时黄色，成功变绿，停止或失败变红", async ({ page }) => {
  let status = "queued";
  await page.route("**/api/v1/videos", (route) => route.fulfill({ json: [
    { id: "idle-video", filename: "待分析.mp4", duration: 30, size_bytes: 100, latest_task: null },
    { id: "working-video", filename: "后台处理.mp4", duration: 30, size_bytes: 100,
      latest_task: taskResponse({ id: "background-task", video_id: "working-video", status, stage: status, config: {}, error: null, warnings: [], timings: {}, notes_state: "none", notes_revision: 0 }) },
  ] }));
  await page.route("**/api/v1/videos/*/evidence*", (route) => route.fulfill({ json: [] }));
  await page.goto("/");
  const item = page.locator(".library-item").filter({ hasText: "后台处理.mp4" });
  await expect(item.locator(".file-glyph")).toHaveCSS("color", "rgb(242, 196, 13)");
  await expect(item).toContainText("排队中");
  status = "running";
  await expect(item.locator(".file-glyph")).toHaveClass(/running/);
  await expect(item.locator(".file-glyph")).toHaveCSS("color", "rgb(242, 196, 13)");
  status = "succeeded";
  await expect(item.locator(".file-glyph")).toHaveCSS("color", "rgb(40, 189, 100)");
  await expect(item).toContainText("已总结");
  for (const [next, label] of [["stopping", "正在停止"], ["paused", "已停止"], ["interrupted", "已中断"], ["failed", "处理失败"]]) {
    status = next;
    await page.reload();
    await expect(item.locator(".file-glyph")).toHaveClass(new RegExp(next));
    await expect(item.locator(".file-glyph")).toHaveCSS("color", "rgb(226, 54, 54)");
    await expect(item).toContainText(label);
  }
  await expect(page.getByRole("button", { name: /待分析.mp4/ }).locator(".file-glyph")).toHaveCSS("color", "rgb(98, 98, 98)");
});

test("停止处理后刷新保持停止，继续完成同一任务", async ({ page }) => {
  const buffer = Buffer.concat([readFileSync(samplePath), Buffer.from(`controls-${Date.now()}`)]);
  const video = await (await page.request.post("/api/v1/videos", { multipart: { file: { name: "停止继续测试.mp4", mimeType: "video/mp4", buffer } } })).json();
  const created = await (await page.request.post(`/api/v1/videos/${video.id}/analyses`)).json();
  await page.goto(`/#summary/${video.id}`);
  await page.getByRole("button", { name: "停止处理", exact: true }).click();
  await expect(page.getByRole("button", { name: "继续处理", exact: true })).toBeVisible();
  expect((await (await page.request.get(`/api/v1/tasks/${created.task_id}`)).json()).status).toBe("paused");
  await page.reload();
  await expect(page.getByRole("button", { name: "继续处理", exact: true })).toBeVisible();
  await page.screenshot({ path: test.info().outputPath("task-paused-desktop.png"), fullPage: true });
  await page.getByRole("button", { name: "继续处理", exact: true }).click();
  await expect(page.locator(".overview p")).toHaveCount(2, { timeout: 15000 });
  const task = await (await page.request.get(`/api/v1/tasks/${created.task_id}`)).json();
  expect(task.status).toBe("succeeded");
  expect(task.attempts).toBe(2);
  expect((await (await page.request.get("/api/v1/videos")).json()).find((item: { id: string }) => item.id === video.id).latest_task.id).toBe(created.task_id);
  await page.request.delete(`/api/v1/videos/${video.id}`);
});

test("侧栏菜单重命名置顶持久化，删除取消和确认", async ({ page }) => {
  const buffer = Buffer.concat([readFileSync(samplePath), Buffer.from(`edit-${Date.now()}`)]);
  const video = await (await page.request.post("/api/v1/videos", { multipart: { file: { name: "视频菜单测试.mp4", mimeType: "video/mp4", buffer } } })).json();
  await page.goto("/#summary");
  const manage = (name: string) => page.locator(".sidebar").getByRole("button", { name: `管理文件：${name}`, exact: true });
  await manage(video.filename).click();
  await page.getByRole("button", { name: "重命名", exact: true }).click();
  const rename = page.getByRole("dialog", { name: "重命名文件" });
  await rename.getByLabel("文件名称").fill("课程笔记测试.mp4");
  await rename.getByRole("button", { name: "保存名称" }).click();
  await expect(rename).not.toBeVisible();
  await manage("课程笔记测试.mp4").click();
  await page.getByRole("button", { name: "置顶", exact: true }).click();
  await expect(page.locator(".library-row").first()).toContainText("课程笔记测试.mp4");
  await expect(page.locator(".library-row").first()).toContainText("置顶");
  await page.reload();
  await expect(page.locator(".library-row").first()).toContainText("课程笔记测试.mp4");
  await manage("课程笔记测试.mp4").click();
  await expect(page.getByRole("button", { name: "取消置顶", exact: true })).toBeVisible();
  await page.screenshot({ path: test.info().outputPath("video-menu-desktop.png") });
  await page.keyboard.press("Escape");
  await manage("课程笔记测试.mp4").click();
  await page.getByRole("button", { name: "删除", exact: true }).click();
  const remove = page.getByRole("dialog", { name: "删除文件？" });
  await remove.getByRole("button", { name: "取消", exact: true }).click();
  await expect(manage("课程笔记测试.mp4")).toBeVisible();
  await manage("课程笔记测试.mp4").click();
  await page.getByRole("button", { name: "删除", exact: true }).click();
  await remove.getByRole("button", { name: "确认删除", exact: true }).click();
  await expect(manage("课程笔记测试.mp4")).toHaveCount(0);
  await expect(remove).not.toBeVisible();
  expect((await page.request.get(`/api/v1/videos/${video.id}/content`)).status()).toBe(404);
});
