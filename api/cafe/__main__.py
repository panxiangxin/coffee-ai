"""在 api 目录执行 python -m cafe；后端端口 8765，Godot MCP Native 使用 9080。"""

import uvicorn


if __name__ == "__main__":
    uvicorn.run("cafe.main:app", host="127.0.0.1", port=8765)
