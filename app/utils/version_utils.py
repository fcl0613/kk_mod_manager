"""版本号展示工具。

manifest 里的版本号可能自带 v/V 前缀（如 "v4.0"），展示层再拼 "v"
会出现 "vv4.0"（Mod 列表、消息、改名文件名均会踩到）。
统一经 display_version 输出，保证前缀唯一且为小写 v。
"""


def display_version(version: str | None) -> str:
    """展示用版本号文本：统一单个小写 v 前缀。

    - "4.0.0"  → "v4.0.0"
    - "v4.0.0" / "V4.0.0" → "v4.0.0"（去重前缀、统一小写）
    - 空值 → ""（避免孤立 "v"）
    """
    v = (version or "").strip()
    if not v:
        return ""
    if v[0] in ("v", "V"):
        return "v" + v[1:]
    return f"v{v}"
