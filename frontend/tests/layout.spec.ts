import { test, expect, type Page } from "@playwright/test";

test.beforeAll(async ({ request }) => {
  const response = await request.get("/__test__/identity");
  expect(response.ok()).toBe(true);
  expect((await response.json()).fake_services_used).toBe(true);
});

async function expectFullTopbar(page: Page) {
  const bounds = await page.locator(".topbar").evaluate((header) => {
    const bar = header.getBoundingClientRect();
    const main = header.parentElement!.getBoundingClientRect();
    return {
      left: bar.left,
      right: bar.right,
      mainLeft: main.left,
      viewport: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth,
    };
  });
  expect(Math.abs(bounds.left - bounds.mainLeft)).toBeLessThan(1);
  expect(Math.abs(bounds.right - bounds.viewport)).toBeLessThan(1);
  expect(bounds.scrollWidth).toBeLessThanOrEqual(bounds.viewport);
}

test("顶部栏在桌面窗口缩窄和侧栏折叠后铺满主区域", async ({ page }) => {
  test.skip(test.info().project.name !== "desktop-1280", "用例内部已遍历完整尺寸矩阵，只执行一次。");
  await page.goto("/#summary");
  for (const width of [1280, 1100, 887, 701, 700, 640]) {
    await page.setViewportSize({ width, height: 871 });
    await expectFullTopbar(page);
    if (width > 700) {
      await page.getByRole("button", { name: "折叠侧边栏" }).click();
      await expect(page.locator(".sidebar")).toBeHidden();
      await expectFullTopbar(page);
      await page.getByRole("button", { name: "展开侧边栏" }).click();
      await expect(page.locator(".sidebar")).toBeVisible();
      await expectFullTopbar(page);
    }
  }
  await page.evaluate(() => window.scrollTo(0, 300));
  await expect(page.locator(".topbar")).toHaveCSS("position", "sticky");
  expect((await page.locator(".topbar").boundingBox())!.y).toBe(0);
  await page.screenshot({ path: test.info().outputPath("topbar-narrow.png") });
});

test("窄桌面窗口侧栏可反复展开关闭并跨宽度切换", async ({ page }) => {
  test.skip(test.info().project.name !== "desktop-1280", "用例内部已遍历完整尺寸矩阵，只执行一次。");
  await page.setViewportSize({ width: 640, height: 871 });
  await page.goto("/#summary");
  const sidebar = page.locator(".sidebar");
  const opener = page.locator(".topbar .sidebar-toggle");
  for (let attempt = 0; attempt < 2; attempt++) {
    await expect(sidebar).not.toBeInViewport();
    await expect(opener).toHaveAttribute("aria-expanded", "false");
    await opener.click();
    await expect(sidebar).toBeInViewport({ ratio: 1 });
    await expect(opener).toHaveAttribute("aria-expanded", "true");
    await sidebar.getByRole("button", { name: "折叠侧边栏" }).click();
    await expect(sidebar).not.toBeInViewport();
    await expect(page.locator(".sidebar-backdrop")).toHaveCount(0);
    await expectFullTopbar(page);
  }
  await opener.click();
  await expect(sidebar).toBeInViewport({ ratio: 1 });
  await page.screenshot({
    path: test.info().outputPath("sidebar-narrow-open.png"),
  });
  await page
    .locator(".sidebar-backdrop")
    .click({ position: { x: 600, y: 300 } });
  await expect(sidebar).not.toBeInViewport();

  await page.setViewportSize({ width: 887, height: 871 });
  await page.getByRole("button", { name: "展开侧边栏" }).click();
  await expect(sidebar).toBeVisible();
  await page.getByRole("button", { name: "折叠侧边栏" }).click();
  await expect(sidebar).toBeHidden();
  await page.reload();
  await expect(sidebar).toBeHidden();
  await page.getByRole("button", { name: "展开侧边栏" }).click();
  await expect(sidebar).toBeVisible();
  await expectFullTopbar(page);
});
