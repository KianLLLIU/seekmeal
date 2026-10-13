"""云端部署入口：监听环境变量 PORT，绑定 0.0.0.0。

本机使用仍走 start.bat / start_web.bat（默认 8000 端口）；
部署到云端沙箱时，平台注入 PORT 环境变量，由此文件启动。
"""

import os

import uvicorn

from diet_assistant.api import app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
