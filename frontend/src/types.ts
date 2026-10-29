import type { TaskStatus } from "./shared/taskStatus";
export type NoteStyle = "concise" | "detailed";
// 回执只确认操作已接受，不能替代随后读取的完整任务状态。
export type TaskReceipt = { task_id: string; status: TaskStatus; reused?: boolean };
export type ActivityError = { code?: string; message?: string; error_id?: string; log_file?: string };
// 后端已记录的动态快照与有限历史，和生成配置、笔记正文分别读取。
export type TaskActivityData = {
  attempt: number;
  sequence: number;
  dropped: number;
  current: { at: string; state: string; message: string; level: string; attempt: number } | null;
  history: { id: number; at: string; state: string; message: string; level: string; attempt: number; error?: ActivityError }[];
  requests: { id: string; operation: string; state: string; attempt: number; network_attempt: number; repair: number;
    chapter_ids: string[]; state_since: string; sent_at: string | null; elapsed_seconds: number }[];
  final_error: ActivityError | null;
};
export type VideoAnalysisPreview = {
  character_count: number | null;
  estimated_segments: number | null;
  reason: string | null;
};
export type NoteChapter = {
  evidence_spans?: { evidence_id: string; start_offset: number; end_offset: number }[];
  chapter_id: string;
  title: string;
  markdown: string;
  evidence_ids: string[];
  start_seconds: number;
  end_seconds: number;
  timestamp_basis: "speech" | "frame" | null;
};
export type SourceIssue = {
  id: string;
  chapter_id: string;
  kind: "source_issue";
  quote: string | null;
  reason: string;
  evidence_ids: string[];
  critical: boolean;
};
export type TranscriptSource = { kind: "asr" | "youtube_subtitle" | "bilibili_subtitle"; language: string; automatic: boolean; digest?: string; track_id?: string };
export function transcriptLabel(source?: TranscriptSource) {
  return source && source.kind !== "asr" ? source.automatic ? "平台自动字幕" : "平台字幕" : "语音识别";
}
// 视频笔记正文契约保留来源、版本及部分覆盖信息，供阅读与导出共用。
export type Result = {
  note_style?: NoteStyle;
  source_cursor?: { evidence_id: string; offset: number } | null;
  recognition?: { transcript_source?: TranscriptSource };
  format_version: 4;
  complete?: boolean;
  covered_until_seconds?: number;
  completed_segments?: number;
  total_segments?: number;
  segment_guides?: {
    index: number;
    start_seconds: number;
    end_seconds: number;
    first_chapter_id: string;
    summary: string;
  }[];
  title: string;
  tags: string[];
  overview_paragraphs: string[];
  chapters: NoteChapter[];
  source_issues: SourceIssue[];
  evidence_ids: string[];
  model: string;
  prompt_version: string;
  coverage_note: string;
  basis: string[];
};
export type SegmentProgress = {
  current: number;
  completed: number;
  total: number;
  start_seconds: number;
  end_seconds: number;
  phase: "planning" | "summarizing" | "issues" | "completed" | "overview";
};
// 任务摘要只承载状态与正文修订号，正文通过独立接口按所选版本读取。
export type Task = {
  activity?: TaskActivityData | null;
  note_style?: NoteStyle;
  progress: {
    steps: { key: string; title: string; description: string }[];
    stage_labels: Record<string, string>;
    timing_labels: Record<string, string>;
    percent: number;
    completed_steps: number;
    current_step: number;
    processed: number;
    total: number | null;
    segmented?: boolean;
    segment?: SegmentProgress | null;
  };
  config: { extract_version?: string; outline_preview?: string; transcript_source?: TranscriptSource };
  id: string;
  video_id: string;
  created_at: string;
  updated_at: string;
  status: TaskStatus;
  notes_state: "ready" | "partial" | "none";
  notes_revision: number;
  segment_progress?: SegmentProgress | null;
  stage: string;
  error: {
    code: string;
    message: string;
    error_id?: string;
    log_file?: string;
  } | null;
  warnings: string[];
  timings: Record<string, number>;
};
export type Video = {
  kind?: "video";
  id: string;
  filename: string;
  duration: number;
  size_bytes: number;
  created_at: string;
  pinned?: boolean;
  media_info?: { container?: string | null } | null;
  latest_task?: Task | null;
};
export type Evidence = {
  id: string;
  kind: string;
  start_seconds: number;
  end_seconds: number;
  text: string;
  image_name: string | null;
  confidence: number | null;
};
export type Health = {
  supported_document_extensions?: string[];
  max_document_bytes?: number;
  recognition?: {
    extract_version: string;
    asr_engine: string;
    ocr_engine: string;
  };
  model_configured: boolean;
  free_tier_confirmed: boolean;
  max_upload_bytes: number;
  max_duration_seconds: number;
  supported_video_extensions?: string[];
  frame_interval_seconds: number;
};

// 提取记录保留源文件与正式材料的身份映射，并提供笔记任务交接编号。
export type DocumentExtraction = {
  source_id: string;
  status: "queued" | "running" | "failed" | "interrupted" | "done";
  parser: "local" | "mineru_agent" | "mineru_vlm";
  progress: Record<string, { stage: string; message: string }>;
  error: { message?: string; error_id?: string; log_file?: string } | null;
  document_id: string | null;
  task_id: string;
};
export type DocumentFile = {
  extraction?: DocumentExtraction | null;
  kind: "document";
  extracted: boolean;
  id: string;
  filename: string;
  format: string;
  size_bytes: number;
  created_at: string;
  pinned: boolean;
  unit_count: number;
  character_count: number;
  warnings: string[];
  latest_task?: DocumentTask | null;
};
export type LibraryFile = Video | DocumentFile;
// 文档材料用稳定编号和来源位置定位，和视频的时间依据分别建模。
export type DocumentUnit = {
  id: string;
  label: string;
  locator: Record<string, string | number>;
  text: string;
  format: "text" | "markdown";
};

export type DocumentTask = {
  activity?: TaskActivityData | null;
  note_style?: NoteStyle;
  id: string;
  document_id: string;
  status: TaskStatus;
  stage: string;
  attempts: number;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  config: { outline_preview?: string; total_segments?: number };
  error: { message?: string; error_id?: string; log_file?: string } | null;
  warnings: string[];
  timings: Record<string, number>;
  notes_state: "none" | "partial" | "ready";
  notes_revision: number;
  request_count: number;
  request_limit: number;
  progress: { stage_labels: Record<string, string>; segment: number; total_segments: number };
};
export type DocumentChapter = {
  chapter_id: string;
  title: string;
  summary: string;
  markdown: string;
  source_ids: string[];
  source_labels: string[];
  source_spans: { unit_id: string; start_offset: number; end_offset: number }[];
};
export type DocumentNotesResult = {
  note_style?: NoteStyle;
  source_cursor?: { unit_id: string; offset: number };
  format_version: 1;
  kind: "document";
  id: string;
  document_id: string;
  title: string;
  tags: string[];
  overview_paragraphs: string[];
  chapters: DocumentChapter[];
  source_issues: { chapter_id: string; quote: string | null; reason: string; source_ids: string[] }[];
  complete: boolean;
  notes_revision: number;
  completed_segments: number;
  total_segments: number;
  covered_until_unit: string;
  covered_until_offset: number;
};
export type DocumentAnalysisPreview = {
  eligible: boolean;
  reason: string | null;
  character_count: number;
  estimated_segments: number;
  limits: { characters: number; segments: number; requests: number };
};

// 设置页和任务确认表单共用识别契约，业务请求不依赖界面模块导出的类型。
export type RecognitionSelection = { asr_option: string; ocr_option: string; language: string };
export type RecognitionModel = {
  id: string;
  kind: "asr" | "ocr";
  name: string;
  description: string;
  directory: string;
  directory_exists: boolean;
  size: string;
  docs: string;
  ready: boolean;
  runtime_ready: boolean;
  download: null | {
    status: string;
    message: string;
    downloaded_bytes: number;
    total_bytes: number | null;
    error?: ActivityError & { log_saved?: boolean };
  };
};
export type RecognitionCatalog = { settings: RecognitionSelection; models: RecognitionModel[]; active_download: string | null };


export type AnalysisSelection = Pick<RecognitionSelection, "asr_option" | "ocr_option"> & { llm_profile_id: string; note_style: NoteStyle };
export type DocumentImportSelection = { llm_profile_id: string; note_style: NoteStyle; ocr_option: string; parser?: "local" | "mineru_agent" | "mineru_vlm" };
// 登录公开状态只说明会话与本机保存能力，不包含 Cookie 内容。
export type BilibiliSession = {
  state: "logged_out" | "logged_in" | "expired" | "unknown";
  persistent: boolean;
  can_persist: boolean;
  warning: string;
};
