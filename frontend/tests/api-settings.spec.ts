import { test, expect } from "@playwright/test";

const root = "/api/v1/settings/model/profiles";
test.beforeEach(async ({ request }) => {
  expect((await (await request.get("/__test__/identity")).json()).fake_services_used).toBe(true);
  await request.post(root + "/default/activate");
  const saved = await (await request.get(root)).json();
  for (const profile of saved.profiles) if (profile.id !== "default") await request.delete(root + "/" + profile.id);
});

test("每组配置保存输入与各阶段输出上限，刷新保留且新配置恢复默认", async ({ page, request }) => {
  await page.goto("/#settings");
  await page.getByRole("button", { name: "添加配置", exact: true }).click();
  await page.getByLabel("配置名称", { exact: true }).fill("自定义预算");
  await page.getByLabel("API 地址", { exact: true }).fill("https://budget.invalid/v1");
  await page.getByLabel("模型 ID", { exact: true }).fill("budget-model");
  await page.getByText("高级设置", { exact: true }).click();
  const inputs = { "生成输入上限（字符）": 22000 };
  const outputs = { outline: 12000, extract: 13000, chapters: 14000, overview: 6000, issue_quotes: 15000, connection_test: 512 };
  const labels = ["章节规划", "信息提取", "正文生成（详尽／简洁）", "整体概览", "存疑引文修正", "连接测试"];
  for (const [label, value] of Object.entries(inputs)) await page.getByLabel(label, { exact: true }).fill(String(value));
  for (const [index, value] of Object.values(outputs).entries()) await page.getByLabel(`${labels[index]}输出上限`, { exact: true }).fill(String(value));
  await page.getByRole("button", { name: "保存配置", exact: true }).click();
  await expect(page.locator(".notice[role='status']")).toContainText("配置已保存");
  const stored = (await (await request.get(root)).json()).profiles.at(-1);
  expect(stored).not.toHaveProperty("outline_text_limit");
  expect(stored.input_text_limit).toBe(22000);
  expect(stored.output_tokens).toEqual(outputs);
  await page.reload();
  await page.getByRole("button", { name: /自定义预算/ }).click();
  await page.getByText("高级设置", { exact: true }).click();
  for (const [label, value] of Object.entries(inputs)) await expect(page.getByLabel(label, { exact: true })).toHaveValue(String(value));
  for (const [index, value] of Object.values(outputs).entries()) await expect(page.getByLabel(`${labels[index]}输出上限`, { exact: true })).toHaveValue(String(value));
  for (const width of [1280, 375]) {
    await page.setViewportSize({ width, height: 871 });
    for (const theme of ["light", "dark"]) {
      await page.locator("html").evaluate((el, value) => el.setAttribute("data-theme", value), theme);
      for (const [index, group] of ["input", "output"].entries()) {
        await page.locator(".api-budget").nth(index).scrollIntoViewIfNeeded();
        await page.screenshot({ path: test.info().outputPath(`budget-${group}-${theme}-${width}.png`), animations: "disabled" });
      }
      const dialog = page.getByRole("dialog", { name: "设置", exact: true });
      expect(await dialog.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
    }
  }
  await page.getByRole("button", { name: "添加配置", exact: true }).click();
  await expect(page.getByLabel("生成输入上限（字符）", { exact: true })).toHaveValue("25000");
  await expect(page.getByLabel("正文生成（详尽／简洁）输出上限", { exact: true })).toHaveValue("32768");
  await expect(page.getByLabel("整体概览输出上限", { exact: true })).toHaveValue("16384");
  await expect(page.getByLabel("连接测试输出上限", { exact: true })).toHaveValue("1024");
});

test("多组连接保存、启用、刷新、编辑、清空密钥和删除相互隔离", async ({ page, request }) => {
  await page.goto("/#settings");
  await expect(page.getByLabel("配置名称")).toHaveValue("魔搭");
  const previous = await (await request.get("/api/v1/settings/model")).json();
  await page.getByRole("button", { name: "添加配置", exact: true }).click();
  await page.getByLabel("配置名称").fill("测试网关");
  await page.getByLabel("API 地址", { exact: true }).fill("https://gateway.invalid/v1");
  await page.getByLabel("API 密钥", { exact: true }).fill("fake-browser-gateway");
  await page.getByLabel("模型 ID", { exact: true }).fill("gateway-model");
  await expect(page.getByRole("checkbox", { name: /我已了解/ })).toHaveCount(0);
  await page.getByRole("button", { name: "保存配置", exact: true }).click();
  await expect(page.locator(".notice[role='status']")).toContainText("配置已保存");
  expect((await (await request.get("/api/v1/settings/model")).json()).model).toBe(previous.model);
  await expect(page.getByLabel("API 密钥", { exact: true })).toHaveValue("");
  expect(await (await request.get(root)).text()).not.toContain("fake-browser-gateway");
  expect(await page.evaluate(() => JSON.stringify(localStorage))).not.toContain("fake-browser-gateway");
  await page.getByRole("button", { name: "启用此配置", exact: true }).click();
  await expect(page.locator(".notice[role='status']")).toContainText("已启用");
  expect((await (await request.get("/api/v1/settings/model")).json()).model).toBe("gateway-model");
  await page.reload();
  await expect(page.getByLabel("配置名称")).toHaveValue("测试网关");
  await expect(page.getByLabel("API 密钥", { exact: true })).toHaveValue("");
  await expect(page.getByRole("button", { name: "删除配置", exact: true })).toBeDisabled();
  await page.getByLabel("模型 ID", { exact: true }).fill("edited-model");
  await page.getByRole("button", { name: "保存配置", exact: true }).click();
  await expect(page.locator(".notice[role='status']")).toContainText("配置已保存");
  expect((await (await request.get("/api/v1/settings/model")).json()).model).toBe("edited-model");
  await page.getByRole("button", { name: "删除已保存密钥", exact: true }).click();
  await expect(page.locator(".notice[role='status']")).toContainText("已删除这组配置的密钥");
  expect((await (await request.get("/api/v1/settings/model")).json()).has_token).toBe(false);
  await page.getByRole("button", { name: new RegExp("魔搭") }).click();
  await page.getByRole("button", { name: "启用此配置", exact: true }).click();
  await expect(page.locator(".notice[role='status']")).toContainText("已启用");
  await page.getByRole("button", { name: /测试网关/ }).click();
  await page.getByRole("button", { name: "删除配置", exact: true }).click();
  await expect(page.locator(".notice[role='status']")).toContainText("已删除配置");
  expect((await (await request.get(root)).json()).profiles).toHaveLength(1);
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "设置", exact: true })).not.toBeVisible();
});

test("未保存的表单可读取模型和测试连接，切换配置后不残留密钥", async ({ page }) => {
  const calls: { path: string; body: any }[] = [];
  await page.route("**/settings/model/profiles/options*", route => {
    calls.push({ path: "options", body: route.request().postDataJSON() });
    return route.fulfill({ json: { models: ["discovered-model"] } });
  });
  await page.route("**/settings/model/profiles/test*", route => {
    calls.push({ path: "test", body: route.request().postDataJSON() });
    return route.fulfill({ json: { ok: true, elapsed_seconds: 0.1 } });
  });
  await page.goto("/#settings");
  await page.getByRole("button", { name: "添加配置", exact: true }).click();
  await page.getByLabel("API 地址", { exact: true }).fill("https://unsaved.invalid/v1");
  await page.getByLabel("API 密钥", { exact: true }).fill("fake-unsaved-browser");
  await page.getByRole("button", { name: "读取可用模型", exact: true }).click();
  await expect(page.locator(".notice[role='status']")).toContainText("已读取 1 个模型");
  await expect(page.getByRole("combobox", { name: "模型 ID", exact: true }).locator("option:not([disabled])"))
    .toHaveAttribute("value", "discovered-model");
  await page.getByLabel("配置名称").fill("未保存网关");
  await page.getByRole("combobox", { name: "模型 ID", exact: true }).selectOption("discovered-model");
  await expect(page.getByRole("checkbox", { name: /我已了解/ })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "测试连接", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "测试连接", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "连接成功" })).toBeVisible();
  expect(calls.map(c => c.path)).toEqual(["options", "test"]);
  for (const call of calls) {
    expect(call.body.base_url).toBe("https://unsaved.invalid/v1");
    expect(call.body.token).toBe("fake-unsaved-browser");
    expect(call.body).not.toHaveProperty("free_tier_confirmed");
  }
  await page.getByRole("button", { name: /魔搭/ }).click();
  await expect(page.getByLabel("API 密钥", { exact: true })).toHaveValue("");
  await expect(page.getByLabel("配置名称")).toHaveValue("魔搭");
  await expect(page.getByRole("button", { name: "手动输入", exact: true })).toHaveCount(0);
});

test("已有模型 ID 不过滤读取的 36 个模型，选择与手填均可保存", async ({ page, request }) => {
  await page.goto("/#settings");
  const modelId = page.getByLabel("模型 ID", { exact: true });
  await expect(modelId).not.toHaveValue("");
  const models = [await modelId.inputValue(), ...Array.from({ length: 35 }, (_, i) => `Qwen/model-${i + 1}`)];
  await page.route("**/settings/model/profiles/options*", route => route.fulfill({ json: { models } }));
  await page.getByLabel("API 密钥", { exact: true }).fill("fake-model-picker");
  await page.getByRole("button", { name: "读取可用模型", exact: true }).click();
  await expect(page.locator(".notice[role='status']")).toContainText("已读取 36 个模型");
  const picker = page.getByRole("combobox", { name: "模型 ID", exact: true });
  await expect(modelId).toHaveCount(1);
  await expect(picker.locator("option:not([disabled])")).toHaveText(models);
  await picker.selectOption("Qwen/model-35");
  await expect(modelId).toHaveValue("Qwen/model-35");
  await page.getByRole("button", { name: "保存配置", exact: true }).click();
  await expect(page.locator(".notice[role='status']")).toContainText("配置已保存");
  expect((await (await request.get("/api/v1/settings/model")).json()).model).toBe("Qwen/model-35");
  await page.getByRole("button", { name: "读取可用模型", exact: true }).click();
  await expect(picker).toBeVisible();
  await page.getByRole("button", { name: "手动输入", exact: true }).click();
  await modelId.fill("manual/unlisted-model");
  await expect(picker).toHaveCount(0);
  await expect(modelId).toHaveCount(1);
  await page.getByRole("button", { name: "从列表选择", exact: true }).click();
  await expect(picker).toHaveValue("manual/unlisted-model");
  await expect(picker.locator("option:not([disabled])")).toHaveCount(37);
  await page.getByRole("button", { name: "手动输入", exact: true }).click();
  await page.getByRole("button", { name: "保存配置", exact: true }).click();
  await expect(page.locator(".notice[role='status']")).toContainText("配置已保存");
  await page.reload();
  await expect(modelId).toHaveValue("manual/unlisted-model");
  await request.put("/api/v1/settings/model", { data: { model: models[0], clear_token: true } });
});

test("窄屏明暗主题下配置选中清晰且与启用状态分离，表单无横向溢出", async ({ page, request }) => {
  const created = await request.post(root, { data: { name: "备用网关", base_url: "https://gateway.invalid/v1", model: "gateway-model" } });
  expect(created.ok()).toBe(true);
  await page.goto("/#settings");
  const active = page.locator(".api-profile").filter({ hasText: "当前使用" });
  const alternate = page.getByRole("button", { name: /备用网关/ });
  for (const width of [375, 598, 1280]) {
    await page.setViewportSize({ width, height: 871 });
    for (const theme of ["light", "dark"]) {
      await page.locator("html").evaluate((el, value) => el.setAttribute("data-theme", value), theme);
      await active.focus();
      await active.press("Enter");
      await expect(active).toHaveAttribute("aria-pressed", "true");
      await expect(active.locator(".api-selected")).toBeVisible();
      await alternate.click();
      await expect(alternate).toHaveAttribute("aria-pressed", "true");
      await expect(alternate.locator(".api-selected")).toHaveText("✓ 正在编辑");
      await expect(active).toHaveAttribute("aria-pressed", "false");
      await expect(active.locator(".api-selected")).toHaveCount(0);
      await expect(page.getByLabel("配置名称")).toHaveValue("备用网关");
      const selectedStyle = await alternate.evaluate(el => ({ border: getComputedStyle(el).borderColor, background: getComputedStyle(el).backgroundColor }));
      const otherStyle = await active.evaluate(el => ({ border: getComputedStyle(el).borderColor, background: getComputedStyle(el).backgroundColor }));
      expect(selectedStyle.border).not.toBe(otherStyle.border);
      expect(selectedStyle.background).not.toBe(otherStyle.background);
      await page.getByText("高级设置", { exact: true }).click();
      await expect(page.getByLabel("并发请求数", { exact: true })).toBeVisible();
      const dialog = page.getByRole("dialog", { name: "设置", exact: true });
      expect(await dialog.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
      await dialog.evaluate(el => { el.scrollTop = 0; });
      await page.screenshot({ path: test.info().outputPath(`api-${theme}-${width}.png`) });
      await page.getByText("高级设置", { exact: true }).click();
    }
  }
  expect((await (await request.get(root)).json()).active_id).toBe("default");
});
