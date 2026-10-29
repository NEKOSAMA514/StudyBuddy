import { test, expect, type Page } from "@playwright/test";
import type { TaskActivityData } from "../src/types";
import { taskResponse } from "./support/taskContract";

async function workspace(page: Page, kind: "video" | "document", style: "concise" | "detailed") {
  const time = "2026-10-24T01:00:00Z";
  const activity: TaskActivityData = { attempt: 1, sequence: 1, dropped: 0, final_error: null,
    current: { at: time, state: "waiting", message: "等待模型返回", level: "info", attempt: 1 },
    history: [{ id: 1, at: time, state: "waiting", message: "请求已发送，等待模型返回", level: "info", attempt: 1 }],
    requests: [{ id: "request-1", operation: style === "concise" ? "concise" : "chapters", state: "waiting", attempt: 1,
      network_attempt: 1, repair: 0, chapter_ids: [], sent_at: time, state_since: time, elapsed_seconds: 42 }] };
  const base = { id: "activity-task", video_id: "activity-file", document_id: "activity-file", status: "running", stage: style === "concise" ? "concise:1/1" : "summarizing",
    note_style: style, notes_state: "none", notes_revision: 0, attempts: 1, config: {}, timings: {}, warnings: [] as string[],
    error: null as { message: string; error_id: string; log_file: string } | null, created_at: time, updated_at: time,
    activity: activity as TaskActivityData | null, request_count: 1, request_limit: 80,
    progress: { stage_labels: { checking: "检查文档材料", planning: "规划文档章节", concise: "生成简洁笔记", summarizing: "生成章节正文", finalizing: "整理概览与校验" }, segment: 1, total_segments: 1 } };
  const state = { task: base, polls: 0, failPoll: false, writes: [] as string[] };
  const task = () => kind === "video" ? taskResponse(state.task) : state.task;
  const file = () => ({ id: "activity-file", kind, filename: "动态测试", size_bytes: 10, duration: 120, format: "txt",
    unit_count: 1, character_count: 10, pinned: false, created_at: time, latest_task: task() });
  await page.route("**/api/v1/**", async route => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() !== "GET") {
      state.writes.push(path);
      if (path.endsWith("/diagnostics/client-errors")) return route.fulfill({ json: { error_id: "network", log_saved: true, message: "轮询连接失败" } });
      if (path.endsWith("/playback")) return route.fulfill({ json: { status: "unprepared", direct: true, original_url: "/empty.mp4", mime_type: "video/mp4", error: null } });
      throw new Error(`Unexpected mutation: ${path}`);
    }
    let json: unknown;
    if (path.endsWith("/health")) json = { model_configured: true, max_upload_bytes: 1000000, max_duration_seconds: 7200, supported_video_extensions: ["mp4"] };
    else if (path === "/api/v1/videos") json = kind === "video" ? [file()] : [];
    else if (path === "/api/v1/documents") json = kind === "document" ? [file()] : [];
    else if (path.endsWith("/analyses")) json = [];
    else if (path.endsWith("/tasks/activity-task") || path.endsWith("/document-tasks/activity-task")) {
      state.polls++;
      if (state.failPoll) return route.fulfill({ status: 503, json: { error: { message: "模拟连接中断", error_id: "poll-failure" } } });
      json = task();
    } else if (path.endsWith("/evidence")) json = [];
    else if (path.endsWith("/playback")) json = { status: "unprepared", direct: true, original_url: "/empty.mp4", mime_type: "video/mp4", error: null };
    else if (path.endsWith("/content")) json = { units: [], total: 0, offset: 0 };
    else throw new Error(`Unexpected read: ${path}`);
    await route.fulfill({ json });
  });
  return { state, activity, open: () => page.goto("/#summary/activity-file") };
}

for (const kind of ["video", "document"] as const) {
  test(`${kind} 继续处理后按最近更新时间前移且刷新保留置顶顺序`, async ({ page }) => {
    const { state } = await workspace(page, kind, "detailed");
    state.task.status = "paused";
    const created = state.task.created_at;
    const file = (id: string, time: string, pinned = false) => {
      const task = id === "activity-file" ? state.task : { ...state.task,
        id: `${id}-task`, video_id: id, document_id: id, created_at: time, updated_at: time, status: "succeeded" };
      return { id, kind, filename: id, pinned, duration: 120, size_bytes: 10, format: "txt", unit_count: 1, character_count: 10,
        created_at: time, latest_task: kind === "video" ? taskResponse(task) : task };
    };
    await page.route("**/api/v1/videos", route => route.fulfill({ json: kind === "video" ? [
      file("activity-file", created), file("newer-file", "2026-10-25T01:00:00Z"), file("pinned-file", created, true),
    ] : [] }));
    await page.route("**/api/v1/documents", route => route.fulfill({ json: kind === "document" ? [
      file("activity-file", created), file("newer-file", "2026-10-25T01:00:00Z"), file("pinned-file", created, true),
    ] : [] }));
    const resume = `/api/v1/${kind === "video" ? "tasks" : "document-tasks"}/activity-task/resume`;
    await page.route(`**${resume}`, async route => {
      state.writes.push(resume);
      state.task.status = "queued";
      state.task.updated_at = "2026-10-26T01:00:00Z";
      await route.fulfill({ json: { task_id: state.task.id, status: state.task.status } });
    });
    await page.clock.install();
    await page.goto("/#summary");
    const names = page.locator(".file-list .file-name");
    await expect(names).toHaveText(["置顶pinned-file", "newer-file", "activity-file"]);
    await names.filter({ hasText: "activity-file" }).click();
    await page.getByRole("button", { name: "继续处理", exact: true }).click();
    await expect(page.locator(".sidebar .library-item")).toHaveText([/pinned-file/, /activity-file/, /newer-file/]);
    await page.goto("/#summary");
    await expect(names).toHaveText(["置顶pinned-file", "activity-file", "newer-file"]);
    await page.reload();
    await expect(names).toHaveText(["置顶pinned-file", "activity-file", "newer-file"]);
    expect(state.task.created_at).toBe(created);
    expect(state.writes.filter(path => path === resume)).toEqual([resume]);
  });
}

for (const style of ["concise", "detailed"] as const) for (const totalSegments of [1, 3]) {
  test(`document/${style}/${totalSegments}段 处理进度复用编号步骤并准确展示阶段与终态`, async ({ page }, testInfo) => {
    const { state, open } = await workspace(page, "document", style);
    state.task.status = "queued";
    state.task.stage = "checking";
    state.task.progress.segment = 0;
    state.task.progress.total_segments = totalSegments;
    await page.clock.install();
    await open();
    const panel = page.locator("#document-generation");
    const steps = panel.locator(".workflow-steps li");
    const progress = panel.getByRole("progressbar", { name: "文档处理步骤完成度" });
    const count = style === "concise" ? 3 : totalSegments > 1 ? 4 : 5;
    await expect(panel.locator(".panel-header h2")).toHaveText("处理进度");
    await expect(panel.locator(".panel-header .status-pill")).toHaveText("排队中");
    await expect(steps).toHaveCount(count);
    await expect(steps.first()).toHaveAttribute("data-done", "true");
    await expect(steps.first()).toContainText("按确认配置提取文字、表格，内嵌图片按需 OCR");
    await expect(steps.nth(1)).toHaveAttribute("aria-current", "step");
    await expect(steps.nth(1).locator(".step-state")).toHaveText("等待中");
    await expect(progress).toHaveAttribute("aria-valuenow", String(Math.round(100 / count)));
    if (style === "concise") {
      await expect(panel.locator('[data-stage="planning"], [data-stage="finalizing"]')).toHaveCount(0);
    }
    await steps.first().evaluate(node => node.setAttribute("data-preserved", "yes"));
    state.task.status = "running";
    state.task.stage = style === "concise" ? "concise" : "planning";
    state.task.progress.segment = Math.min(2, totalSegments);
    await page.clock.runFor(2500);
    await expect(panel.locator('.workflow-steps [aria-current="step"]')).toContainText(`第 ${Math.min(2, totalSegments)}/${totalSegments} 段`);
    await expect(steps.first()).toHaveAttribute("data-preserved", "yes");
    await expect(progress).toHaveAttribute("aria-valuenow", String(Math.round(200 / count)));
    await expect(panel.locator(".workflow-steps .current .step-state")).toHaveText("进行中");
    if (style === "detailed") {
      state.task.stage = "summarizing";
      await page.clock.runFor(2500);
      await expect(panel.locator(`[data-stage="${totalSegments > 1 ? "segments" : "summarizing"}"]`)).toHaveAttribute("aria-current", "step");
      await expect(progress).toHaveAttribute("aria-valuenow", totalSegments > 1 ? "50" : "60");
      if (totalSegments > 1) {
        await expect(panel.locator('[data-stage="segments"]')).toContainText("生成章节正文");
        state.task.stage = "planning";
        state.task.progress.segment = 3;
        await page.clock.runFor(2500);
        await expect(panel.locator('[data-stage="segments"]')).toContainText("第 3/3 段 · 规划文档章节");
        await expect(progress).toHaveAttribute("aria-valuenow", "50");
      }
      state.task.stage = "finalizing";
      await page.clock.runFor(2500);
      await expect(panel.locator('[data-stage="finalizing"]')).toHaveAttribute("aria-current", "step");
      await expect(progress).toHaveAttribute("aria-valuenow", totalSegments > 1 ? "75" : "80");
    }
    for (const [status, text] of [["stopping", "正在停止"], ["paused", "已停止"], ["interrupted", "已停止"], ["failed", "已停止"]]) {
      state.task.status = status;
      await page.reload();
      await expect(panel.locator(".workflow-steps .current .step-state")).toHaveText(text);
      await expect(progress).not.toHaveAttribute("aria-valuenow", "100");
      if (status === "paused" || status === "interrupted") {
        await expect(panel.getByRole("button", { name: "继续处理", exact: true })).toBeEnabled();
      } else if (status === "failed") {
        await expect(panel.getByRole("button", { name: "重试任务", exact: true })).toBeEnabled();
      }
    }
    state.task.status = "succeeded";
    state.task.stage = "completed";
    await page.reload();
    await expect(panel.locator(".panel-header .status-pill")).toHaveText("已完成");
    await expect(progress).toHaveAttribute("aria-valuenow", "100");
    await expect(panel.locator('.workflow-steps [data-done="true"]')).toHaveCount(count);
    await expect(panel.locator('.workflow-steps [aria-current="step"]')).toHaveCount(0);
    await expect(panel).toContainText("已尝试 1/80 次模型请求");
    await expect(panel.getByRole("region", { name: "任务动态", exact: true })).toBeVisible();
    for (const width of [1280, 375]) {
      await page.setViewportSize({ width, height: 871 });
      for (const theme of ["light", "dark"]) {
        await page.evaluate(value => document.documentElement.setAttribute("data-theme", value), theme);
        await panel.screenshot({ path: testInfo.outputPath(`document-progress-${style}-${width}-${theme}.png`), animations: "disabled" });
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
      }
    }
    expect(state.writes).toEqual([]);
  });
}

for (const kind of ["video", "document"] as const) for (const style of ["concise", "detailed"] as const) {
  test(`${kind}/${style} 动态显示等待、并发、校验与持久错误，刷新保留`, async ({ page }) => {
    const { state, activity, open } = await workspace(page, kind, style);
    await open();
    const panel = page.getByRole("region", { name: "任务动态", exact: true });
    await expect(panel).toBeVisible();
    await expect(panel.locator(".activity-heading")).toContainText("等待模型返回");
    await expect(panel.locator(".activity-requests")).toContainText(/00:4[2-9]/);
    state.task.activity = { ...activity, sequence: 2, requests: [
      { ...activity.requests[0], state: "receiving" }, { ...activity.requests[0], id: "request-2", state: "waiting", chapter_ids: ["C2"] }] };
    await expect(panel.locator(".activity-heading")).toContainText("1 个请求接收结果，1 个请求等待模型返回");
    state.task.activity = { ...state.task.activity, sequence: 3, requests: [{ ...activity.requests[0], state: "validating" }],
      history: [...activity.history, { id: 3, at: activity.history[0].at, state: "repair", message: "输出校验未通过，正在修正", level: "warning", attempt: 1,
        error: { error_id: "validation-id", log_file: "log/validation.log" } }] };
    await expect(panel.locator(".activity-heading")).toContainText("校验结果");
    await expect(panel).toContainText("输出校验未通过");
    await expect(panel).not.toContainText("本段已保存");
    state.task.status = "failed";
    state.task.error = { message: "修正后仍未通过校验", error_id: "final-id", log_file: "log/final.log" };
    state.task.activity = { ...state.task.activity, requests: [], final_error: state.task.error };
    await expect(panel.locator(".activity-heading")).toContainText("处理失败");
    await expect(panel).toContainText("final-id");
    await page.reload();
    await expect(panel).toContainText("修正后仍未通过校验");
    await panel.getByText("详细日志", { exact: true }).last().click();
    await expect(panel).toContainText("log/final.log");
    expect(state.writes.filter(path => !path.endsWith("/client-errors") && !path.endsWith("/playback"))).toEqual([]);
  });

  test(`${kind}/${style} 断线重连不丢动态，旧任务不编造历史`, async ({ page }) => {
    const { state, open } = await workspace(page, kind, style);
    state.failPoll = true;
    await open();
    const panel = page.getByRole("region", { name: "任务动态", exact: true });
    await expect(panel).toContainText("正在重新连接");
    await expect(panel).toContainText("请求已发送");
    expect(state.task.status).toBe("running");
    state.failPoll = false;
    await expect(panel).not.toContainText("正在重新连接", { timeout: 10000 });
    state.task.status = "interrupted";
    state.task.activity = null;
    state.task.error = { message: "上次执行已中断", error_id: "old-error", log_file: "log/old.log" };
    state.task.warnings = ["旧任务的材料警告"];
    await page.reload();
    await expect(panel).toContainText("上次执行已中断");
    await expect(panel).toContainText("旧任务的材料警告");
    await expect(panel.locator(".activity-event time")).toHaveCount(0);
    await expect(panel.locator(".activity-requests li")).toHaveCount(0);
    await expect(page.getByText("按实际步骤更新，非预计剩余时间；语音识别期间进度可能暂时不变。")).toHaveCount(0);
  });
}

for (const style of ["concise", "detailed"] as const) {
  test(`文档 ${style} 图片警告按原因合并，位置可展开且不同错误编号保留`, async ({ page }, testInfo) => {
    const { state, open } = await workspace(page, "document", style);
    const reason = "未识别到文字，可能为非文字图片或识别遗漏。";
    const locations = ["第 1 页 · 图片1", "第 1 页 · 图片2", "第 1 页 · 图片4", "第 2 页 · 图片1", "第 2 页 · 图片2"];
    state.task.status = "interrupted";
    state.task.activity = null;
    state.task.warnings = ["PDF 复杂公式可能不完整。", ...locations.map(location => `${location}：${reason}`),
      `${locations[0]}：${reason}`,
      "第 2 页 · 图片3：图片格式无法读取，未识别其中内容。",
      "第 3 页 · 图片1：图片格式无法读取，未识别其中内容。",
      "第 4 页 · 图片1：本地 OCR 读取失败。错误编号：first-error。详细日志：log/first-error.log。",
      "第 5 页 · 图片1：本地 OCR 读取失败。错误编号：second-error。详细日志：log/second-error.log。"];
    await open();
    const panel = page.getByRole("region", { name: "任务动态", exact: true });
    await expect(panel.locator(".activity-warning")).toHaveCount(5);
    const grouped = panel.locator("details.activity-warning").filter({ hasText: reason });
    await expect(grouped).toHaveCount(1);
    await expect(grouped.locator("summary")).toHaveText(`注意：${reason}（共 5 处）`);
    await expect(grouped.locator("p")).not.toBeVisible();
    await grouped.locator("summary").click();
    for (const location of locations) await expect(grouped.locator("p")).toContainText(location);
    await expect(panel.locator("p.activity-warning").filter({ hasText: "first-error" })).toContainText("log/first-error.log");
    await expect(panel.locator("p.activity-warning").filter({ hasText: "second-error" })).toContainText("log/second-error.log");
    await page.reload();
    await expect(grouped.locator("p")).not.toBeVisible();
    for (const width of [1280, 375]) for (const theme of ["light", "dark"]) {
      await page.setViewportSize({ width, height: 871 });
      await page.evaluate(value => { document.documentElement.dataset.theme = value; }, theme);
      await panel.screenshot({ path: testInfo.outputPath(`warnings-${width}-${theme}.png`) });
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    }
    expect(state.writes).toEqual([]);
  });
}

test("动态窗口限制高度，手动滚动不被新记录抢走，亮暗和窄屏可读", async ({ page }, testInfo) => {
  const { state, activity, open } = await workspace(page, "video", "concise");
  const entries = Array.from({ length: 80 }, (_, index) => ({ ...activity.history[0], id: index + 1, message: `阶段动态 ${index + 1}` }));
  state.task.activity = { ...activity, sequence: 80, history: entries, dropped: 20 };
  await open();
  const panel = page.getByRole("region", { name: "任务动态", exact: true });
  const history = panel.getByLabel("任务动态记录");
  await expect.poll(() => history.evaluate(el => el.scrollTop > 0)).toBe(true);
  await history.evaluate(el => { el.scrollTop = 0; el.dispatchEvent(new Event("scroll")); });
  state.task.activity = { ...state.task.activity!, sequence: 81, history: [...entries, { ...entries[0], id: 81, message: "新的任务动态" }] };
  await expect(panel.getByRole("button", { name: "查看最新动态" })).toBeVisible();
  expect(await history.evaluate(el => el.scrollTop)).toBe(0);
  await panel.getByRole("button", { name: "查看最新动态" }).click();
  await expect.poll(() => history.evaluate(el => el.scrollHeight - el.scrollTop - el.clientHeight < 2)).toBe(true);
  for (const width of [1280, 375]) for (const theme of ["light", "dark"]) {
    await page.setViewportSize({ width, height: 871 });
    await page.evaluate(theme => document.documentElement.dataset.theme = theme, theme);
    await panel.evaluate(el => el.scrollIntoView({ block: "center" }));
    const box = await history.boundingBox();
    expect(box!.height).toBe(136);
    expect(await history.locator("li.activity-event").evaluateAll(items => {
      const viewport = items[0].closest(".activity-history")!.getBoundingClientRect();
      return items.filter(item => {
        const row = item.getBoundingClientRect();
        return row.top >= viewport.top - 1 && row.bottom <= viewport.bottom + 1;
      }).length;
    })).toBe(4);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await panel.screenshot({ path: testInfo.outputPath(`activity-${width}-${theme}.png`) });
  }
});
