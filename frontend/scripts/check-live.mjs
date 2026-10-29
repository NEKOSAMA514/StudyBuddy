// Read-only production checks. All browser writes are blocked before they can leave the page.
import { createRequire } from "node:module";
import fs from "node:fs";
import path from "node:path";
const require = createRequire(new URL("../package.json", import.meta.url));
const { chromium, expect } = require("@playwright/test");
const base = new URL(process.argv[2] || "http://127.0.0.1:18000");
if (!["localhost", "127.0.0.1"].includes(base.hostname) || base.protocol !== "http:") {
  throw new Error("Smoke checks only support a local HTTP server.");
}
const output = path.resolve(process.argv[3]);
fs.mkdirSync(output, { recursive: true });
const report = { status: "failed", cloud_model_called: false, existing_results_only: true, viewports: [], layout_viewports: [], errors: [], blocked_writes: [] };
const browser = await chromium.launch();
try {
  for (const width of [531, 887, 1280, 1920]) {
    const page = await browser.newPage({ viewport: { width, height: 871 } });
    page.on("pageerror", error => report.errors.push(error.message));
    await page.route("**/api/v1/**", route => {
      if (!["GET", "HEAD", "OPTIONS"].includes(route.request().method())) {
        report.blocked_writes.push({ method: route.request().method(), path: new URL(route.request().url()).pathname });
        return route.abort();
      }
      return route.continue();
    });
    const healthResponse = await page.request.get(new URL("/api/v1/health", base).href);
    expect(healthResponse.ok()).toBe(true);
    expect((await healthResponse.json()).status).toBe("ok");
    const before = await (await page.request.get(new URL("/api/v1/videos", base).href)).json();
    await page.goto(new URL("/#summary", base).href);
    await expect(page.locator(".sidebar-header .brand")).toHaveText("StudyBuddy");
    await expect(page.locator(".brand-mark")).toHaveCount(0);
    await expect(page.locator(".summary-footnote")).toHaveCount(0);
    await expect(page.getByText("有据可查", { exact: true })).toHaveCount(0);
    const list = page.getByRole("region", { name: "文件列表", exact: true });
    await expect(page.getByRole("region", { name: "文件上传", exact: true })).toBeVisible();
    await expect(list.locator("tbody tr")).toHaveCount(before.length);
    await expect(page.locator(".player-panel")).toHaveCount(0);
    if (before.length) {
      await expect(list.locator(".video-menu-trigger")).toHaveCount(before.length);
      for (const glyph of await page.locator(".video-glyph").all()) {
        await expect(glyph).toHaveText("▶");
        const state = await glyph.getAttribute("class");
        if (state.includes("succeeded")) await expect(glyph).toHaveCSS("color", "rgb(39, 131, 94)");
        if (/queued|running|stopping/.test(state)) await expect(glyph).toHaveCSS("color", "rgb(189, 138, 19)");
      }
      const menuTrigger = list.locator(".video-menu-trigger").first();
      await menuTrigger.scrollIntoViewIfNeeded();
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      await menuTrigger.click();
      const menu = page.locator(".video-action-menu");
      await expect(menu).toBeVisible();
      await expect(menu.getByRole("button")).toHaveText(["重命名", before[0].pinned ? "取消置顶" : "置顶", "删除"]);
      const bounds = await menu.boundingBox();
      expect(bounds.x).toBeGreaterThanOrEqual(0);
      expect(bounds.y + bounds.height).toBeLessThanOrEqual(871);
      await page.screenshot({ path: path.join(output, `live-menu-${width}.png`) });
      await menu.getByRole("button", { name: "重命名", exact: true }).click();
      const rename = page.getByRole("dialog", { name: "重命名文件" });
      await expect(rename.getByLabel("文件名称")).toHaveValue(before[0].filename);
      await rename.getByRole("button", { name: "取消", exact: true }).click();
      await list.locator(".video-menu-trigger").first().click();
      await menu.getByRole("button", { name: "删除", exact: true }).click();
      await page.getByRole("dialog", { name: "删除文件？" }).getByRole("button", { name: "取消", exact: true }).click();
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: path.join(output, `live-summary-${width}.png`) });
    const saved = before.find(video => video.latest_task?.status === "succeeded") || before[0];
    if (saved) {
      await list.locator(".file-name").nth(before.indexOf(saved)).click();
      await expect(page).toHaveURL(new RegExp(`#summary/${saved.id}$`));
      await expect(page.locator(".player-panel h2")).toHaveText(saved.filename);
      await page.reload();
      await expect(page.locator(".player-panel h2")).toHaveText(saved.filename);
      await expect(page.getByRole("button", { name: "重新选择文件", exact: true })).toBeVisible();
      const unusedWidth = await page.locator("main").evaluate(main => {
        const style = getComputedStyle(main);
        return main.getBoundingClientRect().width - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight) - main.querySelector(".player-panel").getBoundingClientRect().width;
      });
      expect(Math.abs(unusedWidth)).toBeLessThan(1);
      await page.locator(".player-panel").screenshot({ path: path.join(output, `live-player-${width}.png`) });
    }
    const overview = page.locator(".overview").first();
    if (await overview.count()) {
      await expect(overview.locator("h3")).not.toHaveText("先看整体");
      await overview.screenshot({ path: path.join(output, `live-overview-${width}.png`) });
    }
    const sidebar = page.locator(".sidebar");
    if (width <= 700 || !(await sidebar.isVisible())) {
      await page.getByRole("button", { name: "展开侧边栏", exact: true }).click();
    }
    await sidebar.getByRole("link", { name: "⚙ 设置", exact: true }).click();
    const close = page.getByRole("button", { name: "关闭设置" });
    await expect(close).toBeVisible();
    const alignment = await close.evaluate(button => {
      const box = button.getBoundingClientRect();
      const icon = button.querySelector("svg").getBoundingClientRect();
      return { x: Math.abs(box.x + box.width / 2 - icon.x - icon.width / 2),
        y: Math.abs(box.y + box.height / 2 - icon.y - icon.height / 2) };
    });
    expect(alignment.x).toBeLessThan(1);
    expect(alignment.y).toBeLessThan(1);
    await expect(page.getByLabel("ModelScope Token")).toHaveValue("");
    const settings = await (await page.request.get(new URL("/api/v1/settings/model", base).href)).json();
    await page.getByRole("tab", { name: "笔记风格", exact: true }).click();
    const preference = page.getByLabel("笔记补充要求（选填）", { exact: true });
    await expect(preference).toHaveValue(settings.summary_prompt);
    await expect(preference).not.toHaveAttribute("required");
    await page.screenshot({ path: path.join(output, `live-settings-${width}.png`) });
    await preference.scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(output, `live-preference-${width}.png`) });
    await close.click();
    await expect(page).toHaveURL(saved ? new RegExp(`#summary/${saved.id}$`) : /#summary$/);
    if (saved) {
      await page.locator(".detail-back").click();
      await expect(list).toBeVisible();
      await expect(page.locator(".player-panel")).toHaveCount(0);
      await page.goBack();
      await expect(page.locator(".player-panel h2")).toHaveText(saved.filename);
      await page.goForward();
      await expect(list).toBeVisible();
    }
    await page.goto(new URL("/", base).href);
    await page.getByRole("button", { name: /开始总结？/ }).click();
    await expect(list).toBeVisible();
    await expect(page.locator(".player-panel")).toHaveCount(0);
    const after = await (await page.request.get(new URL("/api/v1/videos", base).href)).json();
    const identity = list => list.map(item => [item.id, item.filename, item.pinned]);
    expect(identity(after)).toEqual(identity(before));
    report.viewports.push({ width, height: 871, videos_preserved: after.length, menu_checks: before.length ? "passed" : "skipped: empty library" });
    await page.close();
  }
  const layoutPage = await browser.newPage({ viewport: { width: 1280, height: 871 } });
  layoutPage.on("pageerror", error => report.errors.push(error.message));
  await layoutPage.route("**/api/v1/**", route => {
    if (!["GET", "HEAD", "OPTIONS"].includes(route.request().method())) {
      report.blocked_writes.push({ method: route.request().method(), path: new URL(route.request().url()).pathname });
      return route.abort();
    }
    return route.continue();
  });
  await layoutPage.goto(new URL("/#summary", base).href);
  for (const width of [1280, 1100, 887, 701, 700, 640]) {
    await layoutPage.setViewportSize({ width, height: 871 });
    for (const state of ["expanded", "collapsed"]) {
      if (width > 700) {
        const label = state === "expanded" ? "展开侧边栏" : "折叠侧边栏";
        const toggle = layoutPage.getByRole("button", { name: label });
        if (await toggle.isVisible()) await toggle.click();
        const sidebar = layoutPage.locator(".sidebar");
        if (state === "expanded") await expect(sidebar).toBeVisible();
        else await expect(sidebar).toBeHidden();
      }
      const bounds = await layoutPage.locator(".topbar").evaluate(header => {
        const bar = header.getBoundingClientRect();
        return {
          left: bar.left, right: bar.right,
          mainLeft: header.parentElement.getBoundingClientRect().left,
          viewport: document.documentElement.clientWidth,
          scrollWidth: document.documentElement.scrollWidth,
        };
      });
      expect(Math.abs(bounds.left - bounds.mainLeft)).toBeLessThan(1);
      expect(Math.abs(bounds.right - bounds.viewport)).toBeLessThan(1);
      expect(bounds.scrollWidth).toBeLessThanOrEqual(bounds.viewport);
    }
    if (width <= 700) {
      const sidebar = layoutPage.locator(".sidebar");
      const opener = layoutPage.locator(".topbar .sidebar-toggle");
      for (let attempt = 0; attempt < 2; attempt++) {
        await opener.click();
        await expect(sidebar).toBeInViewport({ ratio: 1 });
        await expect(opener).toHaveAttribute("aria-expanded", "true");
        await sidebar.getByRole("button", { name: "折叠侧边栏" }).click();
        await expect(sidebar).not.toBeInViewport();
        await expect(opener).toHaveAttribute("aria-expanded", "false");
        await expect(layoutPage.locator(".sidebar-backdrop")).toHaveCount(0);
      }
    }
    await layoutPage.screenshot({ path: path.join(output, `live-layout-${width}.png`) });
    report.layout_viewports.push({ width, height: 871, topbar: "passed", sidebar: "passed" });
  }
  await layoutPage.close();
  expect(report.errors).toEqual([]);
  expect(report.blocked_writes).toEqual([]);
  report.status = "passed";
} catch (error) {
  report.failure = error.stack || error.message;
  process.exitCode = 1;
} finally {
  fs.writeFileSync(path.join(output, "smoke.json"), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
  await browser.close();
}
