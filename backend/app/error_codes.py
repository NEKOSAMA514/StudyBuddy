"""业务错误分类；请求重试策略保留在调用边界。"""
# 模型错误保留供应商返回的有效原因，未知程序错误使用统一预设摘要。
MODEL_ERRORS = {"model_access_denied", "model_unavailable", "model_request_rejected",
                   "model_response_invalid", "model_stream_error", "model_output_filtered", "model_list_failed"}

KNOWN_ERRORS = {
        "unsupported_extension", "upload_limit", "empty_file", "invalid_length", "invalid_media",
        "duration_limit", "unsupported_codec", "tool_missing", "media_timeout", "frame_missing",
        "model_not_configured", "free_tier_unconfirmed", "model_access_denied", "model_unavailable",
        "model_request_rejected", "model_timeout", "model_output_truncated", "model_incomplete",
        "model_stream_error", "model_response_invalid", "invalid_model_output",
        "unknown_model", "runtime_missing", "model_not_downloaded", "language_unsupported",
        "asr_model_invalid", "model_size_limit", "model_checksum", "invalid_model_path",
        "model_folder_unavailable", "model_delete_failed",
        "download_busy", "model_in_use", "not_found", "not_ready", "wrong_video", "invalid_video",
        "video_in_use", "already_running", "not_stoppable", "not_resumable", "not_retryable",
        "file_missing", "no_evidence", "invalid_evidence", "invalid_timestamp", "local_only",
        "interrupted", "download_interrupted", "settings_load_failed",
        "video_link_invalid", "video_parse_failed", "video_download_failed", "video_download_timeout", "video_output_invalid",
    }
KNOWN_ERRORS.update({"model_input_budget", "processing_failed", "invalid_checkpoint", "media_cancelled", "transcript_save_failed", "checkpoint_cleanup_failed", "generation_changed"})
KNOWN_ERRORS.update({"mineru_token_missing", "mineru_failed", "mineru_download", "mineru_result", "mineru_timeout",
                     "mineru_submission_uncertain", "document_already_prepared"})
KNOWN_ERRORS.update({"document_missing", "document_task_missing", "document_busy", "document_not_prepared",
                     "document_content_missing", "document_source_changed", "document_generation_changed",
                     "document_generation_limit", "document_request_limit", "document_limit", "document_encoding",
                     "document_dependency", "encrypted_document", "invalid_document", "invalid_document_task_state",
                     "notes_unavailable"})

REQUEST_RETRY_ERRORS = frozenset({"model_unavailable", "model_output_filtered", "model_response_invalid",
                                  "model_stream_error", "model_incomplete", "model_timeout"})
