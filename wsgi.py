# -*- coding: utf-8 -*-
"""本地开发启动：python wsgi.py；容器内由 gunicorn 加载。"""
import os

from app import create_app

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=False)
