import { test, expect, type Page } from "@playwright/test";

test.beforeAll(async ({ request }) => {
  expect((await (await request.get("/__test__/identity")).json()).fake_services_used).toBe(true);
});

async function openNetwork(page: Page) {
  const sidebar = page.locator(".sidebar");
  if (!(await sidebar.isVisible()) || page.viewportSize()!.width <= 700)
    await page.getByRole("button", { name: "展开侧边栏", exact: true }).click();
  await sidebar.getByRole("link", { name: "⚙ 设置", exact: true }).click();
  await page.getByRole("tab", { name: "网络代理", exact: true }).click();
  await expect(page.getByLabel("代理地址", { exact: true })).toBeVisible();
}

for (const theme of ["light", "dark"] as const) {
  test(`${theme} 代理设置保存、关闭保留地址、刷新和新页面恢复`, async ({ page, context, request }) => {
    await request.put("/api/v1/settings/network", { data: { enabled: false, url: "" } });
    await page.addInitScript(theme => localStorage.setItem("studybuddy-theme", theme), theme);
    const errors: string[] = [];
    const writes: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    page.on("request", req => { if (req.method() !== "GET") writes.push(new URL(req.url()).pathname); });
    await page.goto("/#summary");
    await openNetwork(page);
    const enabled = page.getByRole("checkbox", { name: "启用视频下载代理" });
    const url = page.getByLabel("代理地址", { exact: true });
    await expect(enabled).not.toBeChecked();
    await expect(url).toHaveValue("");
    await enabled.check();
    await url.fill("socks5h://127.0.0.1:7890");
    await page.getByRole("button", { name: "保存代理设置" }).click();
    await expect(page.locator(".notice[role='status']")).toContainText("正在运行的任务保持原配置");
    expect((await (await request.get("/api/v1/settings/network")).json()).enabled).toBe(true);
    await page.reload();
    await page.getByRole("tab", { name: "网络代理", exact: true }).click();
    await expect(enabled).toBeChecked();
    await expect(url).toHaveValue("socks5h://127.0.0.1:7890");
    await enabled.uncheck();
    await page.getByRole("button", { name: "保存代理设置" }).click();
    await expect(page.locator(".notice[role='status']")).toContainText("已保存");
    const reopened = await context.newPage();
    await reopened.goto("/#settings");
    await reopened.getByRole("tab", { name: "网络代理", exact: true }).click();
    await expect(reopened.getByRole("checkbox", { name: "启用视频下载代理" })).not.toBeChecked();
    await expect(reopened.getByLabel("代理地址", { exact: true })).toHaveValue("socks5h://127.0.0.1:7890");
    await reopened.close();
    expect(writes).toEqual(["/api/v1/settings/network", "/api/v1/settings/network"]);
    expect(errors).toEqual([]);
  });
}

test("两种主题在三种桌面宽度显示完整代理表单", async ({ page, request }) => {
  await request.put("/api/v1/settings/network", { data: { enabled: false, url: "" } });
  await page.goto("/#summary");
  for (const width of [554, 887, 1280]) {
    await page.setViewportSize({ width, height: 871 });
    for (const theme of ["light", "dark"]) {
      if (await page.locator("html").getAttribute("data-theme") !== theme) await page.locator(".theme-toggle").click();
      await openNetwork(page);
      await page.getByLabel("代理地址", { exact: true }).fill("https://127.0.0.1:7890");
      await expect(page.getByRole("tab", { name: "网络代理" })).toBeVisible();
      await expect(page.getByText(/Windows 本机运行可填写/)).toBeVisible();
      const box = await page.getByRole("dialog", { name: "设置", exact: true }).boundingBox();
      expect(box!.x).toBeGreaterThanOrEqual(0);
      expect(box!.x + box!.width).toBeLessThanOrEqual(width);
      await page.screenshot({ path: test.info().outputPath(`network-${theme}-${width}.png`) });
      await page.getByRole("button", { name: "关闭设置", exact: true }).click();
    }
  }
});

test("拒绝认证地址时沿用统一校验通知和错误报告", async ({ page, request }) => {
  await request.put("/api/v1/settings/network", { data: { enabled: false, url: "" } });
  await page.goto("/#summary");
  await openNetwork(page);
  await page.getByRole("checkbox", { name: "启用视频下载代理" }).check();
  await page.getByLabel("代理地址", { exact: true }).fill("http://user:password@localhost:7890");
  await page.getByRole("button", { name: "保存代理设置" }).click();
  await expect(page.getByRole("alert")).toContainText("输入格式错误");
  await expect(page.getByRole("alert")).toContainText("错误编号");
  await expect(page.getByRole("alert")).toContainText("log/");
  await expect(page.getByRole("alert")).not.toContainText("password");
  expect((await (await request.get("/api/v1/settings/network")).json()).enabled).toBe(false);
});
