# 将秒数转换为可读时间戳，供转录和导出共用。
def timestamp(seconds):
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"
